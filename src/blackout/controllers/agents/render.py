"""What the agents read (docs/PRD.md 8.8): the system blocks and the per-turn user message.

**The cached prefix** is ``tools`` (sorted) -> system block 1 -> system block 2:

1. ``system_prompt``: the prompt file (``prompts/single_system.md``, plus
   ``solver_addendum.md`` for the solver variant) with the engine's rules filled in. The same
   for every scenario.
2. ``city_text``: this city's map and gazetteer, and where everything is. Built once from the
   first observation and byte-stable for the rest of the run.

**The user message** of each decision point (``user_message``) holds the rendered observation,
the notes carried over from the last turn (at most 800 characters), the belief table (at most
30 rows) and the last 5 actions with their outcomes.

Report text goes inside ``<report id source_class source channel tick>`` tags with ``<``,
``>`` and ``&`` escaped, so a report can't close its own tag (the injection posture).
"""

from collections.abc import Sequence
from functools import cache
from pathlib import Path

from blackout.controllers.keyword_parser import gazette_of
from blackout.controllers.llm_parser import gazetteer_text
from blackout.info.belief import TELEMETRY, BeliefState, Estimate
from blackout.info.observation import Observation
from blackout.info.reports import ConfirmReply, ReportView
from blackout.sim.config import SimConfig

PROMPTS = Path(__file__).resolve().parent / "prompts"
SYSTEM_FILE = PROMPTS / "single_system.md"
SOLVER_FILE = PROMPTS / "solver_addendum.md"

NOTES_MAX_CHARS = 800
BELIEF_MAX_ROWS = 30
RECENT_ACTIONS = 5


@cache
def prompt_source(solver: bool) -> str:
    """The prompt files as written (their hash is the prompt version, docs/PROMPTS.md)."""
    text = SYSTEM_FILE.read_text(encoding="utf-8")
    if solver:
        text += "\n" + SOLVER_FILE.read_text(encoding="utf-8")
    return text


def system_prompt(solver: bool, config: SimConfig, max_calls: int) -> str:
    """System block 1, with the engine's rules filled in."""
    values = {
        "load": config.load_minutes,
        "unload": config.unload_minutes,
        "heartbeat": config.heartbeat_ticks,
        "ttf_thresholds": ", ".join(str(t) for t in config.ttf_thresholds),
        "trip_grace": config.trip_grace_minutes,
        "reset": config.reset_minutes,
        "w_critical": config.weight_critical,
        "w_essential": config.weight_essential,
        "w_residential": config.weight_residential,
        "confirm_delay": config.confirm_delay_ticks,
        "confirm_limit": config.confirm_limit,
        "max_calls": max_calls,
    }
    text = prompt_source(solver)
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def city_text(observation: Observation) -> str:
    """System block 2: the map with travel times, the gazetteer, and the fixed assets."""
    city = observation.city
    lines = [
        "# This city",
        "",
        f"A grid of {city.width} x {city.height} intersections. Travel times are minutes per"
        " road segment, either way.",
        "",
        "## Travel times",
        "",
    ]
    row: list[str] = []
    for edge_id in sorted(city.edges):
        row.append(f"{edge_id} {city.edges[edge_id].minutes}")
        if len(row) == 10:
            lines.append(", ".join(row))
            row = []
    if row:
        lines.append(", ".join(row))
    lines += ["", gazetteer_text(gazette_of(observation)).rstrip(), "", "## Fixed assets", ""]
    for h in observation.hospitals:
        lines.append(
            f"- Hospital `{h.id}` {h.name}: at `{h.node}`, zone `{h.zone}`, tank {h.tank_l} L"
        )
    for d in observation.depots:
        lines.append(f"- Depot `{d.id}` {d.name}: at `{d.node}`")
    for s in observation.substations:
        lines.append(f"- Substation `{s.id}` {s.name}: at `{s.node}`, capacity {s.capacity_kw} kW")
    for z in observation.zones:
        alternate = f", alternate `{z.alternate}`" if z.alternate else ", no alternate"
        lines.append(
            f"- Zone `{z.id}` {z.name} ({z.priority.value}): primary `{z.primary}`{alternate};"
            f" intersections {' '.join(z.nodes)}"
        )
    for t in observation.trucks:
        lines.append(f"- Truck `{t.id}`: capacity {t.capacity_l} L")
    for c in observation.crews:
        lines.append(f"- Crew `{c.id}`")
    return "\n".join(lines) + "\n"


# --- the observation --------------------------------------------------------------------------


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def report_tag(report: ReportView) -> str:
    source = escape(report.source).replace('"', "'")
    head = (
        f'<report id="{report.id}" source_class="{report.source_class.value}"'
        f' source="{source}" channel="{report.channel.value}" tick="{report.tick}">'
    )
    if report.facts is not None:
        body = "\n".join(f"claim: {f.describe()}" for f in report.facts) or "(no claims)"
    else:
        body = escape(report.text or "")
    return f"{head}\n{body}\n</report>"


def confirm_line(reply: ConfirmReply) -> str:
    checks = "; ".join(
        f"{c.fact.describe()}: {'holds' if c.holds else 'FALSE'}"
        + ("" if c.holds or c.actual is None else f" (actually {c.actual})")
        for c in reply.checks
    )
    return (
        f"- {reply.report_id} (asked t{reply.requested_at}, answered t{reply.tick}):"
        f" {reply.summary()}. {checks}"
    )


def status_sections(observation: Observation) -> dict[str, list[str]]:
    """The telemetry as text, by section (``get_status`` shows one or all of them)."""
    tick = observation.tick
    hospitals = []
    for h in observation.hospitals:
        ttf = "has grid power" if h.ttf_minutes is None else f"dry in ~{h.ttf_minutes} min"
        age = "" if h.as_of == tick else f" (reading from t{h.as_of})"
        hospitals.append(
            f"- {h.id}: {h.status.value}, {'grid' if h.powered else 'NO GRID'};"
            f" fuel {h.fuel_l}/{h.tank_l} L{age}, burn {h.burn_lpm} L/min, {ttf}"
        )
    trucks = []
    for t in observation.trucks:
        where = t.at if t.next_node is None else f"{t.at}>{t.next_node} ({t.elapsed} min in)"
        task = "no task" if t.task is None else f"task {t.task.kind.value} {t.task.target_id}"
        route = f", route {'>'.join(t.route)}" if t.route else ""
        busy = f", {t.work_left} min of work left" if t.work_left else ""
        trucks.append(
            f"- {t.id}: {t.state.value} at {where}, load {t.load_l}/{t.capacity_l} L, {task}"
            f"{route}{busy}"
        )
    crews = []
    for c in observation.crews:
        where = c.at if c.next_node is None else f"{c.at}>{c.next_node} ({c.elapsed} min in)"
        site = f", for {c.substation}" if c.substation else ""
        route = f", route {'>'.join(c.route)}" if c.route else ""
        crews.append(f"- {c.id}: {c.state.value} at {where}{site}{route}")
    substations = []
    for s in observation.substations:
        pct = 0 if not s.capacity_kw else round(100 * s.load_kw / s.capacity_kw)
        extra = ""
        if s.overloaded_minutes:
            extra += f", OVERLOADED {s.overloaded_minutes} min"
        if s.work_left is not None:
            extra += f", {s.work_left} min of crew work left"
        if s.crew:
            extra += f", crew {s.crew} on site"
        substations.append(
            f"- {s.id}: {s.state.value}, load {s.load_kw}/{s.capacity_kw} kW ({pct}%){extra}"
        )
    zones = []
    for z in observation.zones:
        fed = "SHED" if z.shed else (f"fed by {z.fed_by}" if z.fed_by else "UNPOWERED")
        zones.append(f"- {z.id} ({z.priority.value}, {z.demand_kw} kW): {fed}")
    depots = []
    for d in observation.depots:
        age = "" if d.as_of == tick else f" (reading from t{d.as_of})"
        depots.append(f"- {d.id}: stock {d.stock_l} L{age}")
    roads = (
        [f"- blocked: {', '.join(observation.known_blocks)}"] if observation.known_blocks else []
    )
    confirms = [confirm_line(r) for r in observation.confirmations]
    pending = ", ".join(observation.confirms_pending) or "none"
    confirms.append(f"- waiting for: {pending}; confirmations left: {observation.confirms_left}")
    return {
        "hospitals": hospitals or ["- (none in view)"],
        "trucks": trucks or ["- (none in view)"],
        "crews": crews or ["- (none in view)"],
        "substations": substations or ["- (none in view)"],
        "zones": zones or ["- (none in view)"],
        "depots": depots or ["- (none in view)"],
        "roads": roads or ["- no blocks discovered by telemetry"],
        "confirmations": confirms,
    }


SECTION_TITLES = {
    "hospitals": "Hospitals",
    "trucks": "Trucks",
    "crews": "Crews",
    "substations": "Substations",
    "zones": "Zones",
    "depots": "Depots",
    "roads": "Roads (telemetry)",
    "confirmations": "Confirmations",
}


def status_text(observation: Observation, sections: Sequence[str] | None = None) -> str:
    rendered = status_sections(observation)
    lines: list[str] = []
    for name in sections or SECTION_TITLES:
        lines += [f"## {SECTION_TITLES[name]}", *rendered[name], ""]
    return "\n".join(lines).rstrip() + "\n"


# --- the belief table --------------------------------------------------------------------------


def _row(item: str, value: object, estimate: Estimate) -> tuple[int, str]:
    source = "telemetry" if estimate.source == TELEMETRY else estimate.source
    return (
        estimate.as_of,
        f"| {item} | {value} | t{estimate.as_of} | {source} | {estimate.confidence}% |",
    )


def belief_rows(belief: BeliefState, limit: int = BELIEF_MAX_ROWS) -> list[str]:
    """The belief table: hospitals, substations and depots first, then roads, newest first."""
    rows: list[str] = []
    for hid in sorted(belief.hospitals):
        h = belief.hospitals[hid]
        if h.fuel_l is not None:
            now = belief.fuel_now(hid)
            rows.append(_row(f"{hid} fuel now", f"{now} L", h.fuel_l)[1])
        if h.burn_lpm is not None:
            rows.append(_row(f"{hid} burn", f"{h.burn_lpm.value} L/min", h.burn_lpm)[1])
    for sid in sorted(belief.substations):
        s = belief.substations[sid]
        if s.state is not None:
            rows.append(_row(f"{sid} state", s.state.value, s.state)[1])
        if s.eta_minutes is not None and s.eta_minutes.value != 0:
            rows.append(_row(f"{sid} work left", f"{s.eta_minutes.value} min", s.eta_minutes)[1])
    for did in sorted(belief.depots):
        d = belief.depots[did]
        if d.stock_l is not None:
            rows.append(_row(f"{did} stock", f"{d.stock_l.value} L", d.stock_l)[1])
    roads = sorted(
        (_row(f"road {e}", belief.roads[e].value, belief.roads[e]) for e in belief.roads),
        key=lambda r: (-r[0], r[1]),
    )
    rows += [r for _, r in roads]
    header = ["| item | believed | as of | source | confidence |", "|---|---|---|---|---|"]
    if len(rows) > limit:
        rows = [
            *rows[: limit - 1],
            f"| ({len(rows) - limit + 1} older road rows not shown) | | | | |",
        ]
    return header + rows if rows else ["(empty)"]


# --- the user message ---------------------------------------------------------------------------


def user_message(
    observation: Observation,
    belief: BeliefState,
    *,
    decision: int,
    notes: str,
    recent: Sequence[str],
) -> str:
    """The one user message of a decision point."""
    woke = ", ".join(str(t) for t in observation.triggers) or "start"
    lines = [
        f"# Decision point {decision}: t={observation.tick} of {observation.horizon}",
        f"Woken by: {woke}",
        "",
        status_text(observation).rstrip(),
        "",
        "## New reports",
    ]
    if observation.reports:
        lines += [report_tag(r) for r in observation.reports]
    else:
        lines.append("(none)")
    lines += ["", "## Your belief table", *belief_rows(belief), "", "## Your last actions"]
    lines += [f"- {a}" for a in recent] or ["(none yet)"]
    lines += ["", "## Your notes from last turn", notes or "(none)", ""]
    lines.append("Decide now: act with the tools, then call end_turn with your notes.")
    return "\n".join(lines) + "\n"
