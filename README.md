# Blackout

**Claude agents vs a rules script vs an optimization solver, in a simulated city blackout.**

A small simulated city loses power. Different contestants try to keep three hospitals running by coordinating fuel trucks, repair crews and load shedding: a 50-line rules script, an OR-Tools solver, and Claude-based agents (alone, with a solver tool, or as a team). Deterministic Python code is the referee. The goal is an honest, pre-registered answer to one question: *when do AI agents beat the classical approaches, and when don't they?*

> **This is a research sandbox for studying AI coordination, not emergency-response software.**
> The city, the physics and the scenarios are deliberately simplified.

## Status

Milestones **M0 (Foundation)** and **M1 (Engine core and logistics)** of 17 are done. A deterministic city now runs end to end: trucks haul fuel from depots to hospitals, generators burn down, and hidden road blocks stop trucks until somebody reroutes them. The power grid arrives in M2, and AI contestants in M10. The plan is in [docs/PRD.md](docs/PRD.md), and section 12 of it tracks milestone status.

## Try it

```bash
uv run blackout run --scenario hello_blackout --controller scripted --render ascii
uv run blackout run --scenario hello_blackout --quiet   # the do-nothing baseline: 200 dark minutes
```

In the demo, St. Mary's Hospital is on its backup generator with 40 minutes of fuel. The truck takes the fast route, runs into a block on Maple St that nobody knew about, takes the detour, and delivers with 6 minutes to spare.

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
| `src/blackout/sim/` | The engine: map, routing, entities, events, actions, validation, physics |
| `src/blackout/env.py` | The `reset` / `act` / `advance` / `step` API that every contestant drives |
| `src/blackout/controllers/` | Contestants (do-nothing and scripted so far) |
| `scenarios/handmade/` | Hand-made scenarios and their scripts |
| `tests/` | Tests: physics, every rejection reason, property tests, golden run hashes |
| `configs/` | YAML configuration (filled in from M5) |
| `docs/PRD.md` | Product requirements and the milestone plan |
| `docs/DECISIONS.md` | Log of design decisions |

## License

Code is under the [MIT license](LICENSE). Scenario and phrasing data (added from M5) will be CC-BY-4.0.
