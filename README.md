# Blackout

**Claude agents vs a rules script vs an optimization solver, in a simulated city blackout.**

A small simulated city loses power. Different contestants try to keep three hospitals running by coordinating fuel trucks, repair crews and load shedding: a 50-line rules script, an OR-Tools solver, and Claude-based agents (alone, with a solver tool, or as a team). Deterministic Python code is the referee. The goal is an honest, pre-registered answer to one question: *when do AI agents beat the classical approaches, and when don't they?*

> **This is a research sandbox for studying AI coordination, not emergency-response software.**
> The city, the physics and the scenarios are deliberately simplified.

## Status

Milestone **M0 (Foundation)** of 17 is done: the project skeleton, CLI, tests and CI are in place, but nothing is simulated yet. The plan is in [docs/PRD.md](docs/PRD.md), and section 12 of it tracks milestone status.

## Quickstart

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.12 automatically if needed.

```bash
uv sync                       # create .venv and install the locked dependencies
uv run blackout --help        # the command-line interface
uv run pytest                 # tests
uv run ruff check .           # lint
uv run ruff format --check .  # formatting
uv run pyright                # type checks
uv run pre-commit install     # optional: run ruff and hygiene checks on every commit
```

## Repository layout (so far)

| Path | What it is |
|---|---|
| `src/blackout/` | The Python package (only the CLI for now) |
| `tests/` | Tests, including checks that enforce the determinism rules |
| `configs/` | YAML configuration (filled in from M1) |
| `docs/PRD.md` | Product requirements and the milestone plan |
| `docs/DECISIONS.md` | Log of design decisions |

## License

Code is under the [MIT license](LICENSE). Scenario and phrasing data (added from M5) will be CC-BY-4.0.
