# Blackout

**Claude agents vs a rules script vs an optimization solver, in a simulated city blackout.**

A small simulated city loses power. Different contestants try to keep three hospitals running by coordinating fuel trucks, repair crews and load shedding: a 50-line rules script, an OR-Tools solver, and Claude-based agents (alone, with a solver tool, or as a team). Deterministic Python code is the referee. The goal is an honest, pre-registered answer to one question: *when do AI agents beat the classical approaches, and when don't they?*

> **This is a research sandbox for studying AI coordination, not emergency-response software.**
> The city, the physics and the scenarios are deliberately simplified.

## Status

Milestones **M0–M3** of 17 are done: foundation, the engine with its logistics, the power grid, and run logs. A deterministic city now runs end to end:
- Trucks haul fuel from depots to hospitals, and generators burn down.
- Hidden road blocks stop vehicles until somebody reroutes them.
- Substations fail, overload and trip in cascades unless load is shed.
- Crews repair or reset substations.

Every run leaves a small, deterministic log that can be replayed and checked tick by tick, and scored independently of the contestant. AI contestants arrive in M10. The plan is in [docs/PRD.md](docs/PRD.md), and section 12 of it tracks milestone status.

## Try it

```bash
uv run blackout run --scenario hello_blackout --controller scripted --render ascii
uv run blackout run --scenario hello_blackout --quiet   # the do-nothing baseline: 200 dark minutes
uv run blackout run --scenario cascade --controller scripted
uv run blackout run --scenario cascade --quiet          # do nothing: two substations trip
uv run blackout replay runs/adhoc/cascade/scripted__none__r0   # re-simulate and verify a logged run
uv run blackout score runs/adhoc                        # score every logged run, with cross-checks
```

**hello_blackout.** A storm has taken out the East substation, so St. Mary's Hospital is on its backup generator with 40 minutes of fuel. A crew heads out on the two-hour repair. Meanwhile the fuel truck takes the fast route, runs into a block on Maple St that nobody knew about, takes the detour, and delivers with 6 minutes to spare.

**cascade.** When the North substation fails, its zones fall back to East, which can't carry them. Shedding one residential zone at once keeps East up. Doing nothing trips East, then West, and the whole city goes dark.

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
| `src/blackout/sim/` | The engine: map, routing, entities, events, actions, validation, power flow, physics |
| `src/blackout/env.py` | The `reset` / `act` / `advance` / `step` API that every contestant drives |
| `src/blackout/controllers/` | Contestants (do-nothing and scripted so far) |
| `src/blackout/logs/` | Run logs: writer, reader and verified replay |
| `src/blackout/scoring/` | The scorer: metrics from a log alone, cross-checked |
| `scenarios/handmade/` | Hand-made scenarios and their scripts |
| `runs/` | Run logs and summaries (git-ignored) |
| `tests/` | Tests: physics, every rejection reason, property tests, golden run hashes |
| `configs/` | YAML configuration (filled in from M5) |
| `docs/PRD.md` | Product requirements and the milestone plan |
| `docs/DECISIONS.md` | Log of design decisions |

## License

Code is under the [MIT license](LICENSE). Scenario and phrasing data (added from M5) will be CC-BY-4.0.
