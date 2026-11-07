# Blackout

**Claude agents vs a rules script vs an optimization solver, in a simulated city blackout.**

A small simulated city loses power. Different contestants try to keep three hospitals running by coordinating fuel trucks, repair crews and load shedding: a 50-line rules script, an OR-Tools solver, and Claude-based agents (alone, with a solver tool, or as a team). Deterministic Python code is the referee. The goal is an honest, pre-registered answer to one question: *when do AI agents beat the classical approaches, and when don't they?*

> **This is a research sandbox for studying AI coordination, not emergency-response software.**
> The city, the physics and the scenarios are deliberately simplified.

## Status

Milestones **M0–M5** of 17 are done: foundation, the engine with its logistics, the power grid, run logs, the information layer, and the scenario generator. A deterministic city now runs end to end:
- Trucks haul fuel from depots to hospitals, and generators burn down.
- Hidden road blocks stop vehicles until somebody reroutes them.
- Substations fail, overload and trip in cascades unless load is shed.
- Crews repair or reset substations.

Contestants never see the truth, only observations:
- Telemetry, filtered by role (power, hospitals, fuel, transport, or all of them), optionally with delayed hospital and depot readings.
- Radio reports, routed to roles by the channel they came in on.
- Replies to `confirm_report`: verified dispatch checks a report, 8 minutes later, at most 4 times a run.

Every contestant is woken only by changes in its own observations, so a hidden block can't wake anybody; a property test checks this. Each contestant keeps a belief built from what it was told, and a dependency graph built from that belief answers five questions: what depends on X, when does a hospital fail, what's its critical resupply path, what cascades if a substation goes, and which hospitals are at risk.

Scenarios can now be generated from a seed. The generator draws a random city from `configs/city/default.yaml`, a template breaks it, and the radio reports are written at two levels of messiness: structured facts (L0) or free text (L1), with the same facts at the same ticks. The text comes from a phrasing bank (`data/phrasing/train/`), with street names, landmarks, typos, abbreviations, hedges and radio chatter, and every report records its ground truth. The first suite, **dev-v0**, has 18 scenarios (blocked fuel route, cascade, breakdown mid-delivery × L0/L1 × 3 seeds) in `scenarios/dev-v0/`. A sample L1 report:

> Hello? Facilities manager Fischer calling. Uh, the backup tank at the hosp at Birch & 6th is showing 44 liters, and the ICU is on that gen.

Every run leaves a small, deterministic log that can be replayed and checked tick by tick, and scored independently of the contestant. Since M4 the log also records what the contestant believed at each decision. AI contestants arrive in M10. The plan is in [docs/PRD.md](docs/PRD.md), and section 12 of it tracks milestone status.

## Try it

```bash
uv run blackout run --scenario hello_blackout --controller scripted --render ascii
uv run blackout run --scenario hello_blackout --quiet   # the do-nothing baseline: 200 dark minutes
uv run blackout run --scenario cascade --controller scripted
uv run blackout run --scenario cascade --quiet          # do nothing: two substations trip
uv run blackout replay runs/adhoc/cascade/scripted__none__r0   # re-simulate and verify a logged run
uv run blackout score runs/adhoc                        # score every logged run, with cross-checks
uv run blackout gen --suite dev-v0 --sample 20          # regenerate the dev-v0 suite, print 20 L1 reports
uv run blackout gen --suite dev-v0 --check              # check the committed suite matches a fresh run
uv run blackout run --scenario dev0_t01_s0_l1 --quiet   # run a generated scenario
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
| `src/blackout/info/` | The information layer: telemetry, roles, reports, `confirm_report`, wake triggers, beliefs |
| `src/blackout/graph/` | The dependency graph built from a belief, and its five queries |
| `src/blackout/controllers/` | Contestants (do-nothing and scripted so far) |
| `src/blackout/logs/` | Run logs: writer, reader and verified replay |
| `src/blackout/scoring/` | The scorer: metrics from a log alone, cross-checked |
| `src/blackout/scenarios/` | Scenario schema and the generator: seeding, worlds, templates, report text |
| `data/phrasing/train/` | The train phrasing bank (one YAML file per fact type, plus the noise lexicon) |
| `scenarios/dev-v0/` | The generated dev-v0 suite and its manifest of hashes |
| `scenarios/handmade/` | Hand-made scenarios and their scripts |
| `runs/` | Run logs and summaries (git-ignored) |
| `tests/` | Tests: physics, every rejection reason, property tests, golden run hashes |
| `configs/` | YAML configuration: `city/default.yaml` holds the generator's ranges |
| `docs/PRD.md` | Product requirements and the milestone plan |
| `docs/DECISIONS.md` | Log of design decisions |

## License

Code is under the [MIT license](LICENSE). Scenario and phrasing data (`scenarios/`, `data/`) will be CC-BY-4.0 (PRD §9).
