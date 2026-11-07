"""Command-line interface for Blackout.

Commands arrive milestone by milestone; docs/PRD.md section 8.14 lists the full set.
"""

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
from blackout.scenarios.schema import load_scenario
from blackout.sim.actions import ActionResult
from blackout.sim.render_ascii import render_ascii

app = typer.Typer(no_args_is_help=True, add_completion=False)

SCENARIO_DIR = Path("scenarios")


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
) -> None:
    """Run one contestant on one scenario and print what happened."""
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
    result = run_episode(env, contestant, on_decision=None if quiet else show)
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
