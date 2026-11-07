"""Slot filling: the ways a report can name a place, a facility or a number.

Every phrase made here resolves to exactly one thing given the public map (streets, avenues,
landmarks and facility names), and every number phrase is within 10% of the true value (the
parse-F1 tolerance, PRD 8.4). The same functions list aliases for the M6 keyword parser.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from blackout.sim.city import AVENUES, STREETS, City, EdgeId, NodeId

ORDINAL_WORDS = (
    "First",
    "Second",
    "Third",
    "Fourth",
    "Fifth",
    "Sixth",
    "Seventh",
    "Eighth",
    "Ninth",
)
_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
NUMBER_WORDS = frozenset(
    [*_ONES, *_TENS[2:], "hundred", "thousand", "half", "quarter", "a", "an", "and"]
)
"""Words that can carry a reported number; noise operators never touch them."""


def number_words(n: int) -> str:
    """``n`` (0 to 9999) in words, e.g. 250 -> "two hundred fifty"."""
    if not 0 <= n <= 9999:
        raise ValueError(f"number_words handles 0..9999, got {n}")
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if ones == 0 else f"-{_ONES[ones]}")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + ("" if rest == 0 else f" {number_words(rest)}")
    thousands, rest = divmod(n, 1000)
    return f"{_ONES[thousands]} thousand" + ("" if rest == 0 else f" {number_words(rest)}")


def spoken_number(n: int) -> str | None:
    """Radio style, where it exists: 250 -> "two-fifty", 2500 -> "twenty-five hundred liters"."""
    if 110 <= n <= 990 and n % 10 == 0 and n % 100 != 0:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]}-{number_words(rest)}"
    if 1100 <= n <= 9900 and n % 100 == 0 and n % 1000 != 0:
        return f"{number_words(n // 100)} hundred liters"
    return None


def liters_phrases(value: int) -> list[str]:
    """Ways to say ``value`` liters. Rounded forms stay within 5% of the value."""
    forms = [f"{value} liters", f"{value} L", f"{value} litres"]
    if value >= 1000:
        forms.append(f"{value:,} liters")
    if value >= 100:
        rounded = 10 * round(value / 10) if value < 1000 else 100 * round(value / 100)
        forms += [f"about {rounded} liters", f"roughly {rounded} L"]
        spoken = spoken_number(rounded)
        if spoken is not None:
            forms.append(spoken if rounded == value else f"about {spoken}")
        if rounded % 100 == 0 or rounded < 100:
            forms.append(f"{'about ' if rounded != value else ''}{number_words(rounded)} liters")
    else:
        forms.append(f"{number_words(value)} liters")
    return forms


def duration_phrases(minutes: int) -> list[str]:
    """Ways to say a duration of ``minutes``, all exact."""
    forms = [f"{minutes} minutes", f"{minutes} min"]
    hours, rest = divmod(minutes, 60)
    hour_words = "an hour" if hours == 1 else f"{number_words(hours)} hours"
    if hours and rest == 0:
        forms += [hour_words, f"{hours} hours" if hours > 1 else "one hour"]
    elif hours and rest == 30:
        forms.append(
            "an hour and a half" if hours == 1 else f"{number_words(hours)} and a half hours"
        )
        forms.append(f"{hours}.5 hours")
    elif hours:
        forms.append(f"{hour_words} and {number_words(rest)} minutes")
        forms.append(f"{hours}h{rest:02d}")
    return forms


def rate_phrases(lpm: int) -> list[str]:
    return [
        f"{lpm} L/min",
        f"{lpm} liters a minute",
        f"{number_words(lpm)} liters per minute",
        f"{lpm} liters per minute",
    ]


def when_phrases(minutes_ago: int) -> list[str]:
    """A relative time for something that happened ``minutes_ago`` (flavor, not a fact)."""
    if minutes_ago <= 1:
        return ["just now", "a minute ago", "right now"]
    if minutes_ago <= 5:
        return ["a few minutes ago", f"about {minutes_ago} minutes ago", "just a bit ago"]
    rounded = 5 * round(minutes_ago / 5)
    return [f"about {rounded} minutes ago", f"{rounded} minutes back", "a while ago"]


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    node: NodeId


@dataclass(frozen=True, slots=True)
class Gazette:
    """Everything public that a report may name: the map and the facilities on it."""

    city: City
    hospitals: Mapping[str, Place]
    depots: Mapping[str, Place]
    substations: Mapping[str, Place]

    def landmark_at(self, node: NodeId) -> str | None:
        found = sorted(name for name, at in self.city.landmarks.items() if at == node)
        return found[0] if found else None

    def node_phrases(self, node: NodeId) -> list[str]:
        n = self.city.nodes[node]
        street, avenue = STREETS[n.x], AVENUES[n.y]
        forms = [f"{street} & {avenue}", f"{street} and {avenue}", f"{avenue} and {street}"]
        landmark = self.landmark_at(node)
        return forms if landmark is None else [*forms, f"{landmark}"]

    def road_phrases(self, edge: EdgeId) -> list[str]:
        """Ways to name a road segment; each resolves to it alone on this map."""
        e = self.city.edges[edge]
        a, b = self.city.nodes[e.a], self.city.nodes[e.b]  # a is north or west of b
        if a.x == b.x:
            road, first, second = STREETS[a.x], AVENUES[a.y], AVENUES[b.y]
            forms = [
                f"{road} St between {first} and {second}",
                f"{road} between {first} and {second}",
                f"{road} Street between {second} and {first}",
                f"{road} St from {first} down to {second}",
                f"the stretch of {road} between {first} and {second}",
            ]
            near = [(a.id, "south"), (b.id, "north")]
        else:
            road, first, second = AVENUES[a.y], STREETS[a.x], STREETS[b.x]
            spelled = ORDINAL_WORDS[a.y]
            forms = [
                f"{road} Ave between {first} and {second}",
                f"{road} between {first} and {second}",
                f"{spelled} Avenue between {second} and {first}",
                f"{road} Ave from {first} over to {second}",
                f"the stretch of {road} between {first} and {second}",
            ]
            near = [(a.id, "east"), (b.id, "west")]
        kind = "St" if a.x == b.x else "Ave"
        for node, direction in near:
            landmark = self.landmark_at(node)
            if landmark is not None:
                forms.append(f"{road} {kind} just {direction} of {landmark}")
        return forms

    def hospital_phrases(self, hospital_id: str) -> list[str]:
        place = self.hospitals[hospital_id]
        forms = _name_forms(place.name, ("Hospital", "Medical Center"), "hospital")
        return [*forms, *(f"the hospital at {n}" for n in self.node_phrases(place.node)[:1])]

    def depot_phrases(self, depot_id: str) -> list[str]:
        place = self.depots[depot_id]
        kind = "yard" if place.name.endswith("Yard") else "depot"
        forms = _name_forms(place.name, ("Fuel Depot", "Depot", "Fuel Yard"), kind)
        return [*forms, *(f"the {kind} at {n}" for n in self.node_phrases(place.node)[:1])]

    def substation_phrases(self, substation_id: str) -> list[str]:
        place = self.substations[substation_id]
        forms = _name_forms(place.name, ("Substation",), "substation")
        short = place.name.removesuffix(" Substation")
        forms.append(f"{short} sub")
        return [*forms, *(f"the substation at {n}" for n in self.node_phrases(place.node)[:1])]


def _name_forms(name: str, suffixes: tuple[str, ...], kind: str) -> list[str]:
    """The full name, the name without its suffix, and "the <short> <kind>"."""
    forms = [name]
    for suffix in suffixes:
        if name.endswith(" " + suffix):
            short = name.removesuffix(" " + suffix)
            forms.append(short)
            if short.startswith("St. "):
                forms.append("Saint " + short.removeprefix("St. "))
            else:
                forms.append(f"the {short} {kind}")
            break
    return forms
