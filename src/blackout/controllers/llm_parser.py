"""C4 Parser + Solver: an LLM reads the reports, C2 decides (docs/PRD.md 8.7, RQ2).

C4 is C2 Solver with one change: report text (L1 and up) goes to Claude instead of the keyword
parser. Everything else is C2's: the source filter (verified and field facts believed,
unverified ones only once corroborated), confirmations, and ``solve_plan``. At L0 reports
already carry their facts, so C4 makes no calls there and plays exactly like C2.

**One call per report**, stateless, so the calls can be precomputed as a Message Batch:

- system: the parser prompt (``agents/prompts/parser_system.md``, stable across scenarios),
  then the scenario's gazetteer (stable within a run). Both are cache breakpoints, so every
  call after a run's first reads its prefix from the prompt cache.
- user: the report inside a ``<report id source_class source channel tick>`` tag.
- structured output (``ParseOutput``): facts with type, a gazetteer subject ID, a value,
  the model's reading confidence and source assessment, plus ``instruction_like``.

The reply becomes ``Fact`` objects: a subject the gazetteer doesn't list, or a value that
doesn't fit the type, is dropped (and counted). An ``instruction_like`` report contributes no
facts at all (the injection posture). A fault (infra or agent) leaves the report unparsed,
which is logged; the run carries on.

Each decision point's calls go to the run log's ``dp.llm`` (usage, cost and latency as first
recorded, so a cached re-run writes the same log) and the parses to ``dp.info``; the full
replies go to ``transcripts.jsonl.gz``.
"""

from collections.abc import Sequence
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from blackout.controllers.keyword_parser import gazette_of
from blackout.controllers.solver.controller import SolverController
from blackout.info.observation import Observation
from blackout.info.reports import SUBJECT_KIND, SUBSTATION_STATES, Fact, FactType, ReportView
from blackout.llm.client import (
    AGENT_FAULT,
    INFRA_FAULT,
    CallRecord,
    LLMClient,
    LLMRequest,
)
from blackout.llm.config import Effort
from blackout.llm.schemas import strict_schema
from blackout.scenarios.text.slots import Gazette
from blackout.sim.config import SimConfig
from blackout.sim.hashing import digest

PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "prompts" / "parser_system.md"
CONTESTANT = "llm-parser"


class ParsedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: FactType
    subject: str
    amount: int | None
    state: Literal["online", "failed", "tripped", "repairing"] | None
    confidence: Literal["high", "medium", "low"]
    source_assessment: Literal["plausible", "doubtful"]


class ParseOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction_like: bool
    facts: list[ParsedFact]


@cache
def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


@cache
def output_schema() -> dict[str, Any]:
    return strict_schema(ParseOutput)


def prompt_hash() -> str:
    """What makes the parser's prompt: the system text and the output schema."""
    return digest({"system": system_prompt(), "schema": output_schema()})


def gazetteer_text(gazette: Gazette) -> str:
    """The scenario's public gazetteer, as the second (per-scenario) system block."""
    city = gazette.city
    lines = [
        "# This city's gazetteer",
        "",
        "Use these IDs as fact subjects, exactly as written. Intersections are named by a"
        " street (running north-south) and an avenue (running east-west); an intersection ID is"
        " the street's letter and the avenue's number (`C4` is Cedar & 4th).",
        "",
        "## Road segments",
        "",
    ]
    for edge_id in sorted(city.edges):
        phrases = gazette.road_phrases(edge_id)
        lines.append(f"- `{edge_id}`: {phrases[0]}" + _also(phrases[1:]))
    sections = (
        ("Substations", gazette.substations, gazette.substation_phrases),
        ("Hospitals", gazette.hospitals, gazette.hospital_phrases),
        ("Fuel depots", gazette.depots, gazette.depot_phrases),
    )
    for title, places, phrases_of in sections:
        lines += ["", f"## {title}", ""]
        for place_id in sorted(places):
            place = places[place_id]
            at = gazette.node_phrases(place.node)[0]
            names = [p for p in phrases_of(place_id) if p != place.name]
            lines.append(f"- `{place_id}`: {place.name}, at {at} (`{place.node}`)" + _also(names))
    lines += ["", "## Landmarks", ""]
    for name, node in sorted(city.landmarks.items()):
        lines.append(f"- {name}: at {gazette.node_phrases(node)[0]} (`{node}`)")
    return "\n".join(lines) + "\n"


def _also(names: Sequence[str]) -> str:
    return "" if not names else "; also " + ", ".join(f'"{n}"' for n in names)


def report_message(report: ReportView) -> str:
    source = report.source.replace('"', "'")
    return (
        f'<report id="{report.id}" source_class="{report.source_class.value}"'
        f' source="{source}" channel="{report.channel.value}" tick="{report.tick}">\n'
        f"{report.text or ''}\n</report>\n\nExtract the facts this report states."
    )


def parse_request(
    report: ReportView, gazetteer: str, *, model: str, effort: Effort, max_tokens: int
) -> LLMRequest:
    return LLMRequest(
        model=model,
        effort=effort,
        max_tokens=max_tokens,
        system=(system_prompt(), gazetteer),
        messages=({"role": "user", "content": report_message(report)},),
        output_schema=output_schema(),
    )


def known_subjects(gazette: Gazette) -> dict[str, frozenset[str]]:
    return {
        "road": frozenset(gazette.city.edges),
        "substation": frozenset(gazette.substations),
        "hospital": frozenset(gazette.hospitals),
        "depot": frozenset(gazette.depots),
    }


def to_fact(parsed: ParsedFact, known: dict[str, frozenset[str]]) -> Fact | None:
    """The engine's ``Fact`` for a parsed one, or None if its subject or value doesn't fit."""
    kind = SUBJECT_KIND[parsed.type]
    subject = parsed.subject.strip()
    if kind == "road" and subject not in known["road"]:
        a, _, b = subject.partition("-")
        subject = f"{b}-{a}"  # the same segment written the other way round
    if subject not in known[kind]:
        return None
    if kind == "road":
        value: int | str | None = None
    elif parsed.type is FactType.SUBSTATION_STATUS:
        value = parsed.state if parsed.state in SUBSTATION_STATES else None
    else:
        value = parsed.amount
    try:
        return Fact(type=parsed.type, subject=subject, value=value)
    except ValidationError:
        return None


class LLMParserController(SolverController):
    """C4. ``client`` decides where replies come from (live, cache, fixtures)."""

    name = CONTESTANT

    def __init__(
        self,
        client: LLMClient,
        *,
        model: str,
        effort: Effort,
        run_index: int = 0,
        config: SimConfig | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.effort: Effort = effort
        self.run_index = run_index
        self.max_tokens = client.models.max_tokens(model, "parser")
        self.prompt_hash = prompt_hash()
        super().__init__(config)

    def reset(self) -> None:
        super().reset()
        self._gazetteer: str | None = None
        self._known: dict[str, frozenset[str]] = {}
        self._calls: list[CallRecord] = []
        self._parses: list[dict[str, Any]] = []
        self._transcript: list[dict[str, Any]] = []
        self._all_calls: list[CallRecord] = []
        self._dp = 0

    def decide(self, observation: Observation) -> Sequence[Any]:
        self._calls, self._parses = [], []
        actions = super().decide(observation)
        self._dp += 1
        return actions

    # --- parsing -----------------------------------------------------------------------------

    def _facts(self, observation: Observation, report: ReportView) -> Sequence[Fact]:
        if report.facts is not None:
            return [*report.facts]
        if not report.text:
            return []
        if self._gazetteer is None:
            gazette = gazette_of(observation)
            self._gazetteer = gazetteer_text(gazette)
            self._known = known_subjects(gazette)
        request = parse_request(
            report,
            self._gazetteer,
            model=self.model,
            effort=self.effort,
            max_tokens=self.max_tokens,
        )
        result = self.client.call(request, run_index=self.run_index)
        record = result.record
        output: ParseOutput | None = None
        if result.ok:
            try:
                output = ParseOutput.model_validate_json(result.text())
            except ValidationError:
                record = replace(
                    record, fault=AGENT_FAULT, fault_detail="reply isn't valid ParseOutput JSON"
                )
        self._calls.append(record)
        self._all_calls.append(record)
        self._transcript.append(
            {
                "dp": self._dp,
                "report": report.id,
                "user": report_message(report),
                "call": record.to_dict(),
                "content": result.response.get("content") if result.response else None,
            }
        )
        entry: dict[str, Any] = {"report": report.id, "key": record.key, "facts": []}
        if output is None:
            entry["fault"] = record.fault
            entry["detail"] = record.fault_detail
            self._parses.append(entry)
            return []
        facts: list[Fact] = []
        dropped = 0
        for parsed in output.facts:
            fact = to_fact(parsed, self._known)
            if fact is None:
                dropped += 1
                continue
            if fact not in facts:
                facts.append(fact)
            entry["facts"].append(
                {
                    **fact.model_dump(mode="json"),
                    "confidence": parsed.confidence,
                    "source_assessment": parsed.source_assessment,
                }
            )
        entry["instruction_like"] = output.instruction_like
        entry["dropped"] = dropped
        self._parses.append(entry)
        if output.instruction_like:
            return []  # an instruction-like report contributes nothing to belief
        return facts

    # --- logging hooks -------------------------------------------------------------------------

    def decision_log(self) -> dict[str, Any] | None:
        """This decision point's ``dp.llm`` and ``dp.info`` (None if it made no calls)."""
        if not self._calls:
            return None
        return {"llm": llm_summary(self._calls), "info": {"parses": self._parses}}

    def transcripts(self) -> list[dict[str, Any]]:
        return list(self._transcript)

    def run_stats(self) -> dict[str, object]:
        stats = super().run_stats()
        totals = llm_summary(self._all_calls)
        for per_call in ("cache_keys", "stop_reasons"):  # in the run log, per decision point
            totals.pop(per_call)
        stats["llm"] = {
            **totals,
            "spend_usd": round(sum(c.spend_usd for c in self._all_calls), 6),
            "sources": _count(c.source for c in self._all_calls),
            "infra_fault_pct": _pct(
                sum(1 for c in self._all_calls if c.fault == INFRA_FAULT), len(self._all_calls)
            ),
            "fallback_run": any(c.fallback for c in self._all_calls),
        }
        return stats


def llm_summary(calls: Sequence[CallRecord]) -> dict[str, Any]:
    """Totals in the shape of the run log's ``LlmRecord``."""
    return {
        "calls": len(calls),
        "input_tokens": sum(c.usage.input_tokens for c in calls),
        "output_tokens": sum(c.usage.output_tokens for c in calls),
        "cache_creation_input_tokens": sum(c.usage.cache_creation_input_tokens for c in calls),
        "cache_read_input_tokens": sum(c.usage.cache_read_input_tokens for c in calls),
        "cost_usd": round(sum(c.cost_usd for c in calls), 6),
        "latency_ms": sum(c.latency_ms for c in calls),
        "served_models": sorted({c.served_model for c in calls if c.served_model}),
        "stop_reasons": [c.stop_reason or "none" for c in calls],
        "cache_keys": [c.key for c in calls],
        "agent_faults": sum(1 for c in calls if c.fault == AGENT_FAULT),
        "infra_faults": sum(1 for c in calls if c.fault == INFRA_FAULT),
        "refusals": sum(1 for c in calls if c.stop_reason == "refusal"),
        "fallback_calls": sum(1 for c in calls if c.fallback),
    }


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _pct(part: int, whole: int) -> float:
    return 0.0 if not whole else round(100 * part / whole, 2)
