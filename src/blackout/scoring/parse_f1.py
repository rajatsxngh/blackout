"""Parse F1: how well a parser turns report text back into its facts (docs/PRD.md 8.4, 8.10).

A parsed fact counts as correct if a claim of the same report has the same kind and subject,
and a value within 10% (or the same state). Each claim can be matched once. Parsing is about
reading, so a false report's claims count like a true one's (M8); an injection claims nothing.
At L0 contestants get the facts themselves, so the "parse" there is exact by construction.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from blackout.controllers.keyword_parser import KeywordParser
from blackout.info.reports import Fact, close_enough
from blackout.logs.reader import RunLog
from blackout.scenarios.schema import Scenario
from blackout.scenarios.text.slots import Gazette, Place
from blackout.sim.city import build_grid_city


@dataclass(frozen=True, slots=True)
class ParseCounts:
    """True positives, false positives and false negatives over a set of reports."""

    tp: int = 0
    fp: int = 0
    fn: int = 0

    def __add__(self, other: "ParseCounts") -> "ParseCounts":
        return ParseCounts(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        total = 2 * self.tp + self.fp + self.fn
        return 2 * self.tp / total if total else None


def matches(claimed: Fact, true: Fact) -> bool:
    """Same kind and subject, and the value within 10% (PRD 8.4's parse-F1 rule)."""
    if claimed.type is not true.type or claimed.subject != true.subject:
        return False
    if isinstance(claimed.value, int) and isinstance(true.value, int):
        return close_enough(claimed.value, true.value)
    return claimed.value == true.value


def score_parse(parsed: Iterable[Fact], truth: Iterable[Fact]) -> ParseCounts:
    """Counts for one report: each true fact can be matched by at most one parsed fact."""
    left = list(truth)
    tp = fp = 0
    for fact in parsed:
        hit = next((i for i, true in enumerate(left) if matches(fact, true)), None)
        if hit is None:
            fp += 1
        else:
            tp += 1
            left.pop(hit)
    return ParseCounts(tp, fp, len(left))


def parse_f1(scenarios: Iterable[Scenario]) -> Mapping[int, ParseCounts]:
    """Keyword-parser counts per level over the reports of ``scenarios`` (facts at L0)."""
    totals: dict[int, ParseCounts] = {}
    for scenario in scenarios:
        parser: KeywordParser | None = None
        for event in scenario.events:
            if event.kind != "report":
                continue
            if scenario.level == 0 or event.text is None:
                parsed: tuple[Fact, ...] = tuple(event.facts)
            else:
                if parser is None:
                    parser = KeywordParser(scenario_gazette(scenario))
                parsed = parser.parse(event.text)
            truth = list(event.facts)  # what it claims, true or not (none for injections)
            counts = score_parse(parsed, truth)
            totals[scenario.level] = totals.get(scenario.level, ParseCounts()) + counts
    return dict(sorted(totals.items()))


def scenario_gazette(scenario: Scenario) -> Gazette:
    """The public gazetteer of a scenario: its map and the names of its facilities."""
    spec = scenario.city
    city = build_grid_city(
        spec.width,
        spec.height,
        default_minutes=spec.default_travel_minutes,
        minutes=spec.travel_minutes,
        landmarks=spec.landmarks,
    )
    return Gazette(
        city=city,
        hospitals={h.id: Place(h.name, h.node) for h in scenario.hospitals},
        depots={d.id: Place(d.name, d.node) for d in scenario.depots},
        substations={s.id: Place(s.name, s.node) for s in scenario.substations},
    )


def logged_parse_counts(log: RunLog) -> ParseCounts:
    """An LLM parser's counts over one run, from its log alone (C4, M10).

    Every report the parser was asked about is in some ``dp.info.parses`` entry, with the facts
    it read (none if its call faulted). They're scored like the keyword parser's, against the
    claims the embedded scenario says each report makes.
    """
    scenario = log.scenario()
    claims = {e.id: list(e.facts) for e in scenario.events if e.kind == "report"}
    total = ParseCounts()
    for dp in log.decisions:
        parses = (dp.info or {}).get("parses", [])
        for entry in parses:
            parsed = [
                Fact(type=f["type"], subject=f["subject"], value=f["value"])
                for f in entry.get("facts", [])
            ]
            total += score_parse(parsed, claims.get(entry["report"], []))
    return total
