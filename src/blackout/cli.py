"""Command-line interface for Blackout.

Commands arrive milestone by milestone; docs/PRD.md section 8.14 lists the full set.
"""

import platform
import sys
from typing import Annotated

import typer

from blackout import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


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
