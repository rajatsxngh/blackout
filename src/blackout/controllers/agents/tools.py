"""The agents' tools (docs/PRD.md 8.8): information, belief, every 8.3 action, and the solver.

Every tool but the two solver tools is strict (``strict: true``; schemas from
``blackout.llm.schemas``, so every field is required and an optional one is ``null``).
``call_solver`` and ``accept_plan`` use the same schemas without ``strict``: with all twelve
enforced, the API rejects C6's tool set as a too-large compiled grammar (D110). Inputs are
validated again here, by building the Pydantic model, so a non-strict tool, a test double or
a schema gap can't slip a bad value through (an invalid one is an ``is_error`` result).

- ``get_status(scope)``: the current state (after this turn's actions), one section or all.
- ``query_graph(query, subject, mode, max_slack)``: the five dependency-graph queries, from
  the agent's belief.
- ``update_belief(report_id, verdict, facts)``: the agent's judgment of one report; accepted
  facts go into its ``BeliefState``.
- the action tools (``dispatch_truck``, ``reroute``, ``assign_crew``, ``shed_load``,
  ``restore_load``, ``confirm_report``) call ``env.act()`` immediately, through the
  ``Actor``. A rejection comes back as an ``is_error`` result and counts as an invalid action
  (the engine counts it); so does an input that doesn't make a valid action.
- ``end_turn(notes)``: ends the decision point; notes (cut at 800 characters) carry over.
- solver variant only: ``call_solver(overrides)`` (at most 2 per decision point) returns a
  ``SolverResult`` rendered in about 400 tokens, and ``accept_plan(plan_id, reason, edits)``
  carries out a plan from this decision point. The solver never acts on its own.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from blackout.controllers.agents.render import (
    NOTES_MAX_CHARS,
    SECTION_TITLES,
    report_tag,
    status_text,
)
from blackout.controllers.base import Actor
from blackout.controllers.llm_parser import ParsedFact, to_fact
from blackout.controllers.solver.contract import SolverBeliefs, SolverResult, solve_plan
from blackout.graph import queries
from blackout.graph.dependency import CriticalPath, build_graph
from blackout.info.belief import BeliefState, trust_all
from blackout.info.reports import Fact, FactType
from blackout.llm.loop import ToolError, ToolHandler
from blackout.llm.schemas import strict_tool
from blackout.sim.actions import (
    REASON_MAX_CHARS,
    ActionResult,
    AnyAction,
    AssignCrew,
    ConfirmReport,
    DispatchTruck,
    Reroute,
    RestoreLoad,
    ShedLoad,
    vehicle_of,
)
from blackout.sim.config import SimConfig
from blackout.sim.entities import TaskKind

SOLVER_CALLS_PER_DECISION = 2
REPORTS_SHOWN = 20

# --- inputs ----------------------------------------------------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


_REASON = Field(description="Why, in a few words (cut at 140 characters; shown in the replay).")
_ROUTE = Field(
    description="Exact path as intersection IDs, starting where the vehicle can next turn;"
    " null to let the engine plan it."
)
_AVOID = Field(
    description="Road segment IDs the engine must route around (blocks you believe from"
    " reports); null or [] for none. Give route or avoid_edges, not both."
)


class GetStatusIn(_In):
    scope: Literal[
        "all",
        "hospitals",
        "trucks",
        "crews",
        "substations",
        "zones",
        "depots",
        "roads",
        "confirmations",
        "reports",
    ] = Field(description="One section, 'all' for every telemetry section, or 'reports'.")


class QueryGraphIn(_In):
    query: Literal["at_risk", "time_to_failure", "critical_path", "cascade_check", "dependents"]
    subject: str | None = Field(
        description="A hospital ID (time_to_failure, critical_path), a substation ID"
        " (cascade_check) or a graph node ID (dependents); null for at_risk."
    )
    mode: Literal["fail", "trip"] | None = Field(
        description="cascade_check only: what happens to the substation (default fail)."
    )
    max_slack: int | None = Field(description="at_risk only: minutes of slack (default 30).")


class ClaimIn(_In):
    type: FactType
    subject: str = Field(description="The ID from the gazetteer (road segment, substation...).")
    amount: int | None = Field(
        description="Liters, L/min or minutes for numeric facts; null otherwise."
    )
    state: Literal["online", "failed", "tripped", "repairing"] | None = Field(
        description="substation_status only; null otherwise."
    )


class UpdateBeliefIn(_In):
    report_id: str
    verdict: Literal["accept", "reject", "unsure"] = Field(
        description="accept: believe the facts listed; reject: believe none of the report;"
        " unsure: believe none for now."
    )
    facts: list[ClaimIn] = Field(description="accept only: the claims you believe; else [].")


class DispatchTruckIn(_In):
    truck_id: str
    task: TaskKind = Field(description="load at a depot, deliver to a hospital, or move.")
    target_id: str = Field(description="A depot, a hospital, or an intersection ID.")
    amount_l: int | None = Field(description="Liters (more than 0); null for the most.")
    route: list[str] | None = _ROUTE
    avoid_edges: list[str] | None = _AVOID
    reason: str = _REASON


class RerouteIn(_In):
    vehicle_id: str
    route: list[str] | None = _ROUTE
    avoid_edges: list[str] | None = _AVOID
    reason: str = _REASON


class AssignCrewIn(_In):
    crew_id: str
    substation_id: str
    route: list[str] | None = _ROUTE
    avoid_edges: list[str] | None = _AVOID
    reason: str = _REASON


class ZoneIn(_In):
    zone_id: str
    reason: str = _REASON


class ConfirmReportIn(_In):
    report_id: str
    reason: str = _REASON


class EndTurnIn(_In):
    notes: str | None = Field(
        description="Notes for your next turn (cut at 800 characters); null keeps the last ones."
    )


class HospitalValue(_In):
    hospital: str
    value: int


class TruckHospital(_In):
    truck: str
    hospital: str


class OverridesIn(_In):
    blocked_roads: list[str] = Field(description="Road segments to plan as blocked.")
    cleared_roads: list[str] = Field(description="Road segments to plan as open.")
    fuel_l: list[HospitalValue] = Field(description="A hospital's liters in the tank now.")
    burn_lpm: list[HospitalValue] = Field(description="A hospital's burn rate, L/min.")
    priority: list[HospitalValue] = Field(
        description="A hospital's weight in the objective (default 100 for critical zones)."
    )
    unavailable_trucks: list[str] = Field(description="Trucks the plan must leave idle.")
    pin: list[TruckHospital] = Field(description="Truck -> hospital its next trip must serve.")


class CallSolverIn(_In):
    overrides: OverridesIn


class EditsIn(_In):
    skip: list[str] = Field(description="Truck, crew or zone IDs whose actions to leave out.")


class AcceptPlanIn(_In):
    plan_id: str
    reason: str = Field(description="Why you accept it (put in front of each action's reason).")
    edits: EditsIn | None


# --- definitions ---------------------------------------------------------------------------

_DEFINITIONS: dict[str, tuple[str, type[BaseModel]]] = {
    "get_status": (
        "Re-read the current state, including what your actions this turn did. Use it after"
        " acting if you need to check, not to re-read the observation you were just shown."
        " scope 'reports' lists the reports received so far (the latest 20) with your verdicts.",
        GetStatusIn,
    ),
    "query_graph": (
        "Ask the dependency graph, built from your belief table. at_risk: hospitals on"
        " generator with little or no slack, worst first. time_to_failure: believed minutes"
        " until a hospital goes dark. critical_path: the believed fastest resupply (truck,"
        " depot, route, ETA vs deadline, slack). cascade_check: what else goes down if a"
        " substation fails or trips. dependents: everything that depends on a node.",
        QueryGraphIn,
    ),
    "update_belief": (
        "Record your judgment of one report. With verdict accept, the facts you list go into"
        " your belief table (and the planning queries) with the report's source reliability."
        " Use the gazetteer IDs as subjects. reject or unsure records that you don't believe it"
        " (pass facts []).",
        UpdateBeliefIn,
    ),
    "dispatch_truck": (
        "Send a truck to load at a depot, deliver to a hospital, or move to an intersection."
        " Applied immediately. Rejected if the truck is loading, unloading or broken, if it"
        " would deliver while empty, if an ID is unknown, or if the truck already moved this"
        " decision point.",
        DispatchTruckIn,
    ),
    "reroute": (
        "Give a truck or crew a new path to where it's already going. Applied immediately.",
        RerouteIn,
    ),
    "assign_crew": (
        "Send a crew to a substation: it repairs a failed one, resets a tripped one, or waits"
        " at a working one. Applied immediately. Rejected while the crew is mid-repair.",
        AssignCrewIn,
    ),
    "shed_load": (
        "Disconnect a zone now, taking its demand off its substation (relieves an overload)."
        " Shedding a critical zone puts its hospital on its generator.",
        ZoneIn,
    ),
    "restore_load": ("Reconnect a zone that was shed. Applied immediately.", ZoneIn),
    "confirm_report": (
        "Ask verified dispatch to check every claim of a report. The true answer arrives 8"
        " minutes later. At most 4 per run: spend them on reports that change what you do.",
        ConfirmReportIn,
    ),
    "end_turn": (
        "End this decision point; the clock runs until the next one. Leave notes for your next"
        " turn: what you're waiting for, which reports you doubt, your plan.",
        EndTurnIn,
    ),
    "call_solver": (
        "Plan power and fuel with the CP-SAT solver from your belief table, with optional"
        " overrides (pass [] for none). Returns a plan ID, each truck's trips with ETAs, the"
        " predicted dark minutes per hospital, the power plan and the actions that would"
        " start it. Nothing is applied. At most 2 calls per decision point.",
        CallSolverIn,
    ),
    "accept_plan": (
        "Carry out a plan returned by call_solver at this decision point: its actions are"
        " applied immediately, in order, and each result comes back. edits.skip leaves out the"
        " actions for the trucks, crews or zones you name (null for no edits).",
        AcceptPlanIn,
    ),
}

BASE_TOOLS = (
    "get_status",
    "query_graph",
    "update_belief",
    "dispatch_truck",
    "reroute",
    "assign_crew",
    "shed_load",
    "restore_load",
    "confirm_report",
    "end_turn",
)
SOLVER_TOOLS = ("call_solver", "accept_plan")
ACTION_TOOLS = frozenset(
    {"dispatch_truck", "reroute", "assign_crew", "shed_load", "restore_load", "confirm_report"}
)


@cache
def tool_definitions(solver: bool) -> tuple[dict[str, Any], ...]:
    """The strict tool definitions, sorted by name (the cached prefix starts with them)."""
    names = sorted(BASE_TOOLS + (SOLVER_TOOLS if solver else ()))
    return tuple(
        strict_tool(n, _DEFINITIONS[n][0], _DEFINITIONS[n][1], strict=n not in SOLVER_TOOLS)
        for n in names
    )


# --- the handlers --------------------------------------------------------------------------


def _reason(text: str) -> str:
    return text.strip()[:REASON_MAX_CHARS]


def _path(route: list[str] | None) -> tuple[str, ...] | None:
    return tuple(route) if route else None


def _describe_path(path: CriticalPath) -> str:
    deadline = "has grid power" if path.deadline is None else f"dark in {path.deadline} min"
    if path.truck is None:
        return f"{path.hospital}: {deadline}; NO truck can reach it on roads believed open"
    via = f"via {path.depot}" if path.depot else "direct (loaded)"
    route = ">".join(path.route)
    return (
        f"{path.hospital}: {deadline}; best {path.truck} {via}, fuel in {path.eta} min,"
        f" slack {path.slack} min, route {route}"
    )


@dataclass(slots=True)
class TurnTools:
    """One decision point's tools, bound to the actor and the agent's persistent belief.

    It counts what happened: tool errors, invalid action inputs (the engine counts the
    rejections it makes itself), solver calls, plans, verdicts, and whether the turn ended.
    """

    actor: Actor
    belief: BeliefState
    config: SimConfig
    known: dict[str, frozenset[str]]
    verdicts: dict[str, str]
    solver: bool = False
    previous_plan: SolverResult | None = None
    notes: str | None = None
    ended: bool = False
    invalid_inputs: int = 0
    rejected: int = 0
    tool_errors: int = 0
    solver_calls: int = 0
    plans: dict[str, SolverResult] = field(default_factory=dict)
    accepted: list[str] = field(default_factory=list)
    new_verdicts: dict[str, str] = field(default_factory=dict)
    taken: list[ActionResult] = field(default_factory=list)
    solve_stats: list[dict[str, object]] = field(default_factory=list)
    used: list[str] = field(default_factory=list)

    def handlers(self) -> dict[str, ToolHandler]:
        names = BASE_TOOLS + (SOLVER_TOOLS if self.solver else ())
        return {name: self._wrap(name) for name in names}

    def _wrap(self, name: str) -> ToolHandler:
        method: Callable[[dict[str, Any]], str] = getattr(self, f"_{name}")
        model = _DEFINITIONS[name][1]

        def handler(args: dict[str, Any]) -> str:
            self.used.append(name)
            try:
                try:
                    parsed = model.model_validate(args)
                except ValidationError as error:
                    if name in ACTION_TOOLS:
                        self.invalid_inputs += 1
                    raise ToolError(f"invalid input: {_errors(error)}") from error
                return method(parsed.model_dump())
            except ToolError:
                self.tool_errors += 1
                raise

        return handler

    # --- information ------------------------------------------------------------------------

    def _refresh(self) -> None:
        """Bring the belief's telemetry up to now (this turn's actions included)."""
        self.belief.apply_telemetry(self.actor.observe())

    def _get_status(self, args: dict[str, Any]) -> str:
        observation = self.actor.observe()
        self.belief.apply_telemetry(observation)
        scope = args["scope"]
        if scope == "reports":
            reports = sorted(self.belief.reports.values(), key=lambda r: (r.tick, r.id))
            if not reports:
                return "No reports yet."
            shown = reports[-REPORTS_SHOWN:]
            lines = [f"{len(reports)} reports so far; the latest {len(shown)}:"]
            for report in shown:
                verdict = self.verdicts.get(report.id, "not judged")
                lines.append(f"[your verdict: {verdict}]\n{report_tag(report)}")
            return "\n".join(lines)
        sections = None if scope == "all" else [scope]
        assert sections is None or sections[0] in SECTION_TITLES
        return f"t={observation.tick}\n" + status_text(observation, sections)

    def _query_graph(self, args: dict[str, Any]) -> str:
        self._refresh()
        graph = build_graph(self.belief, self.config)
        query, subject = args["query"], args["subject"]
        try:
            if query == "at_risk":
                slack = 30 if args["max_slack"] is None else args["max_slack"]
                paths = queries.at_risk(graph, slack)
                if not paths:
                    return f"No hospital on generator with {slack} min of slack or less."
                return "\n".join(_describe_path(p) for p in paths)
            if subject is None:
                raise ToolError(f"{query} needs a subject")
            if query == "time_to_failure":
                ttf = queries.time_to_failure(graph, subject)
                return f"{subject}: " + ("has grid power" if ttf is None else f"{ttf} min")
            if query == "critical_path":
                return _describe_path(queries.critical_path(graph, subject))
            if query == "cascade_check":
                mode = args["mode"] or "fail"
                result = queries.cascade_check(graph, subject, mode)
                trips = " then ".join(", ".join(w) for w in result.trips) or "nothing else"
                restore = (
                    "unknown" if result.restore_minutes is None else f"{result.restore_minutes}"
                )
                return (
                    f"If {subject} {'fails' if mode == 'fail' else 'trips'}: trips {trips};"
                    f" unpowered zones {', '.join(result.unpowered_zones) or 'none'};"
                    f" hospitals on generator {', '.join(result.hospitals_on_generator) or 'none'};"
                    f" lost {result.lost_kw} kW (weighted {result.lost_weighted_kw});"
                    f" restore takes {restore} min once a crew is on site"
                )
            nodes = queries.dependents(graph, subject)
            return f"{subject} <- " + (", ".join(nodes) or "nothing")
        except ValueError as error:
            raise ToolError(str(error)) from error

    # --- belief -------------------------------------------------------------------------------

    def _update_belief(self, args: dict[str, Any]) -> str:
        report_id, verdict = args["report_id"], args["verdict"]
        report = self.belief.reports.get(report_id)
        if report is None:
            raise ToolError(f"no report {report_id!r} has been received")
        if verdict != "accept":
            self.verdicts[report_id] = self.new_verdicts[report_id] = verdict
            return f"{report_id}: recorded as {verdict}; nothing believed from it"
        facts: list[Fact] = []
        bad: list[str] = []
        for claim in args["facts"]:
            parsed = ParsedFact(
                type=claim["type"],
                subject=claim["subject"],
                amount=claim["amount"],
                state=claim["state"],
                confidence="high",
                source_assessment="plausible",
            )
            fact = to_fact(parsed, self.known)
            if fact is None:
                bad.append(f"{claim['type']} {claim['subject']}")
            elif fact not in facts:
                facts.append(fact)
        if bad:
            raise ToolError(
                f"these claims don't fit a known ID or value, nothing recorded: {'; '.join(bad)}"
            )
        if not facts:
            raise ToolError("accept needs at least one fact (or use reject/unsure)")
        self.belief.apply_facts(facts, trust_all, report=report)
        self.verdicts[report_id] = self.new_verdicts[report_id] = verdict
        return f"{report_id}: believed " + "; ".join(f.describe() for f in facts)

    # --- actions -----------------------------------------------------------------------------

    def _act(self, action: AnyAction) -> str:
        result = self.actor.act(action)
        self.taken.append(result)
        if not result.accepted:
            self.rejected += 1
            raise ToolError(result.summary())
        return result.summary()

    def _build(self, make: Callable[[], AnyAction]) -> AnyAction:
        try:
            return make()
        except ValidationError as error:
            self.invalid_inputs += 1
            raise ToolError(f"invalid action: {_errors(error)}") from error

    def _dispatch_truck(self, a: dict[str, Any]) -> str:
        return self._act(
            self._build(
                lambda: DispatchTruck(
                    truck_id=a["truck_id"],
                    task=a["task"],
                    target_id=a["target_id"],
                    amount_l=a["amount_l"],
                    route=_path(a["route"]),
                    avoid_edges=tuple(a["avoid_edges"] or ()),
                    reason=_reason(a["reason"]),
                )
            )
        )

    def _reroute(self, a: dict[str, Any]) -> str:
        return self._act(
            self._build(
                lambda: Reroute(
                    vehicle_id=a["vehicle_id"],
                    route=_path(a["route"]),
                    avoid_edges=tuple(a["avoid_edges"] or ()),
                    reason=_reason(a["reason"]),
                )
            )
        )

    def _assign_crew(self, a: dict[str, Any]) -> str:
        return self._act(
            self._build(
                lambda: AssignCrew(
                    crew_id=a["crew_id"],
                    substation_id=a["substation_id"],
                    route=_path(a["route"]),
                    avoid_edges=tuple(a["avoid_edges"] or ()),
                    reason=_reason(a["reason"]),
                )
            )
        )

    def _shed_load(self, a: dict[str, Any]) -> str:
        return self._act(ShedLoad(zone_id=a["zone_id"], reason=_reason(a["reason"])))

    def _restore_load(self, a: dict[str, Any]) -> str:
        return self._act(RestoreLoad(zone_id=a["zone_id"], reason=_reason(a["reason"])))

    def _confirm_report(self, a: dict[str, Any]) -> str:
        return self._act(ConfirmReport(report_id=a["report_id"], reason=_reason(a["reason"])))

    def _end_turn(self, a: dict[str, Any]) -> str:
        self.ended = True
        if a["notes"] is not None:
            self.notes = a["notes"].strip()[:NOTES_MAX_CHARS]
        return "Turn ended."

    # --- the solver ---------------------------------------------------------------------------

    def _overrides(self, o: dict[str, Any]) -> SolverBeliefs:
        roads, hospitals = self.known["road"], frozenset(self.belief.hospitals)
        trucks = frozenset(self.belief.trucks)
        problems: list[str] = []

        def road(segment: str) -> str:
            if segment in roads:
                return segment
            a, _, b = segment.partition("-")
            if f"{b}-{a}" in roads:
                return f"{b}-{a}"
            problems.append(f"unknown road {segment!r}")
            return segment

        def hospital(hid: str) -> str:
            if hid not in hospitals:
                problems.append(f"unknown hospital {hid!r}")
            return hid

        def truck(tid: str) -> str:
            if tid not in trucks:
                problems.append(f"unknown truck {tid!r}")
            return tid

        def values(items: list[dict[str, Any]], what: str) -> dict[str, int]:
            out: dict[str, int] = {}
            for item in items:
                if item["value"] < 0:
                    problems.append(f"{what} for {item['hospital']} can't be negative")
                out[hospital(item["hospital"])] = item["value"]
            return dict(sorted(out.items()))

        beliefs = SolverBeliefs(
            blocked_roads=tuple(sorted({road(r) for r in o["blocked_roads"]})),
            cleared_roads=tuple(sorted({road(r) for r in o["cleared_roads"]})),
            fuel_l=values(o["fuel_l"], "fuel_l"),
            burn_lpm=values(o["burn_lpm"], "burn_lpm"),
            priority=values(o["priority"], "priority"),
            unavailable_trucks=tuple(sorted({truck(t) for t in o["unavailable_trucks"]})),
            pin=dict(sorted((truck(p["truck"]), hospital(p["hospital"])) for p in o["pin"])),
        )
        if problems:
            raise ToolError("bad overrides, nothing planned: " + "; ".join(problems))
        return beliefs

    def _call_solver(self, a: dict[str, Any]) -> str:
        if self.solver_calls >= SOLVER_CALLS_PER_DECISION:
            raise ToolError(
                f"solver limit reached: at most {SOLVER_CALLS_PER_DECISION} calls per decision"
                " point. Accept one of this turn's plans or act yourself."
            )
        overrides = self._overrides(a["overrides"])
        self._refresh()
        self.solver_calls += 1
        try:
            plan = solve_plan(
                self.belief, overrides, config=self.config, previous=self.previous_plan
            )
        except ValueError as error:
            raise ToolError(f"the solver can't plan: {error}") from error
        self.plans[plan.plan_id] = plan
        self.previous_plan = plan
        stats = plan.stats
        self.solve_stats.append(
            {"tick": plan.tick, "status": stats.status, "wall_time_s": stats.wall_time_s}
        )
        return plan.render()

    def _accept_plan(self, a: dict[str, Any]) -> str:
        plan_id = a["plan_id"]
        plan = self.plans.get(plan_id)
        if plan is None:
            known = ", ".join(self.plans) or "none"
            raise ToolError(f"no plan {plan_id!r} from this decision point (plans: {known})")
        if plan_id in self.accepted:
            raise ToolError(f"plan {plan_id} was already accepted")
        self.accepted.append(plan_id)
        skip = set((a["edits"] or {}).get("skip", ()))
        why = a["reason"].strip()
        lines: list[str] = []
        rejected = applied = 0
        for action in plan.actions:
            subject = _subject(action)
            if subject in skip:
                lines.append(f"skipped {action.kind} for {subject}")
                continue
            reason = _reason(f"{why} | {action.reason}" if why else action.reason)
            result = self.actor.act(action.model_copy(update={"reason": reason}))
            self.taken.append(result)
            if result.accepted:
                applied += 1
            else:
                rejected += 1
            lines.append(result.summary())
        if not plan.actions:
            lines.append("the plan proposes no actions now: everything is already on its way")
        self.rejected += rejected
        if rejected:
            raise ToolError(
                f"{rejected} action(s) rejected ({applied} applied):\n" + "\n".join(lines)
            )
        return f"plan {plan_id} accepted:\n" + "\n".join(lines)


def _subject(action: AnyAction) -> str:
    if isinstance(action, ShedLoad | RestoreLoad):
        return action.zone_id
    if isinstance(action, ConfirmReport):
        return action.report_id
    return vehicle_of(action)


def _errors(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        where = ".".join(str(p) for p in item["loc"]) or "input"
        parts.append(f"{where}: {item['msg']}")
    return "; ".join(parts)
