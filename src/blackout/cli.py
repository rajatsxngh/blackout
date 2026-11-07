"""Command-line interface for Blackout.

Commands arrive milestone by milestone; docs/PRD.md section 8.14 lists the full set.
"""

import json
import platform
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from blackout import __version__
from blackout.controllers.base import Controller
from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.scripted import ScriptedController, load_script
from blackout.env import BlackoutEnv, DecisionRequest, run_episode
from blackout.logs.reader import read_log
from blackout.logs.replay import replay
from blackout.logs.schema import LOG_FILE, LogError
from blackout.logs.writer import record_episode, run_dir_for
from blackout.scenarios.schema import load_scenario
from blackout.scoring.scorer import ScoreCard, score
from blackout.sim.actions import ActionResult
from blackout.sim.render_ascii import render_ascii

app = typer.Typer(no_args_is_help=True, add_completion=False)

SCENARIO_DIR = Path("scenarios")
DEFAULT_OUT = Path("runs") / "adhoc"


class ControllerName(StrEnum):
    DO_NOTHING = "do-nothing"
    SCRIPTED = "scripted"


class RenderMode(StrEnum):
    NONE = "none"
    ASCII = "ascii"


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"blackout {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Blackout: Claude agents vs a rules script vs an optimization solver in a simulated city
    blackout. A research sandbox, not emergency-response software.
    """


@app.command()
def info() -> None:
    """Show version and environment details (useful in bug reports)."""
    typer.echo(f"blackout {__version__}")
    typer.echo(f"python   {platform.python_version()} ({sys.implementation.name})")
    typer.echo(f"platform {platform.platform()}")


@app.command()
def run(
    scenario: Annotated[
        str,
        typer.Option("--scenario", "-s", help="A scenario file, or the ID of one in ./scenarios."),
    ],
    controller: Annotated[
        ControllerName, typer.Option("--controller", "-c", help="Who makes the decisions.")
    ] = ControllerName.DO_NOTHING,
    script: Annotated[
        Path | None,
        typer.Option(help="Script for the scripted controller [default: <scenario>.script.json]."),
    ] = None,
    render: Annotated[
        RenderMode, typer.Option(help="Draw the city at every decision point.")
    ] = RenderMode.NONE,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Print only the summary.")] = False,
    out: Annotated[
        Path, typer.Option(help="Where run logs go: <out>/<scenario>/<contestant>__<model>__r0/.")
    ] = DEFAULT_OUT,
    log: Annotated[bool, typer.Option("--log/--no-log", help="Write a run log.")] = True,
) -> None:
    """Run one contestant on one scenario, print what happened, and log it."""
    scenario_path = _find_scenario(scenario)
    loaded = _load(load_scenario, scenario_path, "scenario")
    contestant = _make_controller(controller, script or scenario_path.with_suffix(".script.json"))
    env = BlackoutEnv(loaded)

    def show(request: DecisionRequest, results: tuple[ActionResult, ...]) -> None:
        typer.echo(f"t={request.tick:>3}  {', '.join(str(t) for t in request.triggers)}")
        for result in results:
            typer.echo(f"       {result.summary()}")
        if render is RenderMode.ASCII:
            typer.echo(render_ascii(env.engine) + "\n")

    typer.echo(f"{loaded.id}: {contestant.name}, horizon {loaded.horizon} min")
    shown = None if quiet else show
    log_path: Path | None = None
    if log:
        run_dir = run_dir_for(out, loaded.id, contestant.name)
        recorded = record_episode(env, contestant, run_dir, on_decision=shown)
        result, log_path = recorded.episode, recorded.log_path
    else:
        result = run_episode(env, contestant, on_decision=shown)
    m = env.engine.metrics
    rejected = sum(m.actions_rejected.values())
    typer.echo("")
    if render is RenderMode.ASCII:
        typer.echo(render_ascii(env.engine) + "\n")
    typer.echo(
        f"Done at t={result.done.tick} ({result.done.reason}), {m.decision_points} decisions"
    )
    typer.echo(
        f"  critical downtime  {m.critical_downtime} min"
        f" (hospitals that lost power: {m.hospitals_lost_power})"
    )
    typer.echo(
        f"  fuel               loaded {m.fuel_loaded_l} L, delivered {m.fuel_delivered_l} L,"
        f" burned {m.fuel_burned_l} L"
    )
    typer.echo(
        f"  driving            {m.drive_minutes} min over {m.edges_traversed} road segments,"
        f" stuck {m.stuck_events} time(s)"
    )
    restored = env.engine.report()["power_restored_at"]
    typer.echo(
        f"  power              unmet {m.unmet_kw_min} kW-min, {m.trips} trip(s),"
        f" {m.repairs_completed} repair(s), fully restored at"
        f" {'never' if restored is None else f't={restored}'}"
    )
    typer.echo(f"  actions            {m.actions_accepted} accepted, {rejected} rejected")
    typer.echo(f"  run hash           {result.run_hash}")
    if log_path is not None:
        size = log_path.stat().st_size
        typer.echo(f"  log                {log_path} ({size / 1024:.1f} KB)")


@app.command("replay")
def replay_command(
    path: Annotated[Path, typer.Argument(help="A run directory, or its run.jsonl.gz.")],
) -> None:
    """Re-simulate a logged run and check every state, decision and action against the log."""
    try:
        log = read_log(path)
        result = replay(log)
    except LogError as error:
        typer.echo(f"replay failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    episode = result.episode
    typer.echo(
        f"{log.header.run_id}: verified {len(episode.tick_hashes)} ticks"
        f" and {len(episode.decisions)} decision points"
    )
    typer.echo(f"  run hash  {episode.run_hash}")


@app.command("score")
def score_command(
    paths: Annotated[
        list[Path], typer.Argument(help="Run directories, run logs, or folders to search.")
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Print score cards as JSON.")] = False,
) -> None:
    """Score runs from their logs alone, cross-checking each against its end record."""
    logs = _collect_logs(paths)
    if not logs:
        raise typer.BadParameter("no run logs found", param_hint="'PATHS...'")
    cards: list[ScoreCard] = []
    failed = 0
    for log_path in logs:
        try:
            card = score(log_path)
        except LogError as error:
            failed += 1
            typer.echo(f"{log_path}: cannot score: {error}", err=True)
            continue
        cards.append(card)
        failed += 0 if card.ok else 1
    if as_json:
        typer.echo(json.dumps([card.to_dict() for card in cards], indent=2, sort_keys=True))
    else:
        for card in cards:
            typer.echo(_score_line(card))
            for problem in card.discrepancies:
                typer.echo(f"    discrepancy: {problem}")
    if failed:
        raise typer.Exit(code=1)


def _collect_logs(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_file():
            found.append(path)
        elif (path / LOG_FILE).is_file():
            found.append(path / LOG_FILE)
        elif path.is_dir():
            found.extend(sorted(path.rglob(LOG_FILE)))
    return found


def _score_line(card: ScoreCard) -> str:
    rejected = sum(card.actions_rejected.values())
    restored = "never" if card.power_restored_at is None else f"t={card.power_restored_at}"
    status = "ok" if card.ok else f"{len(card.discrepancies)} DISCREPANCIES"
    return (
        f"{card.run_id}: downtime {card.critical_downtime_min} min,"
        f" unmet {card.unmet_kw_min} kW-min, restored {restored},"
        f" {card.actions} actions ({rejected} rejected) - {status}"
    )


def _find_scenario(name: str) -> Path:
    path = Path(name)
    if path.is_file():
        return path
    matches = sorted(SCENARIO_DIR.glob(f"**/{name}.json"))
    if len(matches) == 1:
        return matches[0]
    problem = "no scenario file or ID" if not matches else f"{len(matches)} scenarios have ID"
    raise typer.BadParameter(
        f"{problem} {name!r} (looked in {SCENARIO_DIR}/)", param_hint="'--scenario'"
    )


def _make_controller(name: ControllerName, script_path: Path) -> Controller:
    if name is ControllerName.SCRIPTED:
        if not script_path.is_file():
            raise typer.BadParameter(f"script not found: {script_path}", param_hint="'--script'")
        return ScriptedController(_load(load_script, script_path, "script"))
    return DoNothingController()


def _load[T](loader: Callable[[Path], T], path: Path, what: str) -> T:
    try:
        return loader(path)
    except ValidationError as error:
        raise typer.BadParameter(f"invalid {what} {path}:\n{error}") from error
