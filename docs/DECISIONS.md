# Decision log

Every design decision gets one entry (docs/PRD.md §10), newest last. To change a decision, add a new entry that supersedes the old one; don't edit history.

| ID | Date | Decision | Why | Alternatives considered |
|---|---|---|---|---|
| D001 | 2026-11-07 | Official `anthropic` Python SDK behind a thin `LLMClient` interface | One provider; full access to prompt caching, effort, strict tools, structured outputs, batches and usage fields | LiteLLM (can lag behind new parameters) |
| D002 | 2026-11-07 | Solver baseline uses OR-Tools CP-SAT over enumerated truck plans | Exact at this size; deterministic with `num_workers=1` and a deterministic time limit; optimizes dark minutes directly | OR-Tools routing API (needs workarounds for reloads, finite stock and draining tanks; its local search stops on wall-clock limits) |
| D003 | 2026-11-07 | Static replays only; no live or WebSocket mode | Demos use replays, so there's no live API cost or lag | FastAPI + WebSockets |
| D004 | 2026-11-07 | JSONL run logs instead of a tracing service | No extra service to run; the logs already hold everything and drive replay | OpenTelemetry + Langfuse |
| D005 | 2026-11-07 | Viewer built with Vite + React + TypeScript + Canvas 2D | Simplest option for fewer than 50 animated entities | PixiJS |
| D006 | 2026-11-07 | Contestants are woken only by their own observation stream | A wake-up caused by hidden truth would leak information | Triggers computed from ground truth |
| D007 | 2026-11-07 | Every false report carries at least one cue, plus a `confirm_report` action | Careful reading can then win at L2, not just blanket distrust | False reports with no detectable cue |
| D008 | 2026-11-07 | Haiku 5.5 for building and for the complete design; Opus 5.5 coverage decided by priority fill within budget caps | Cost per run is uncertain (could be 2–3× the estimate), and this keeps every research question answerable within $400 | A fixed full matrix on Opus |
| D009 | 2026-11-07 | Python pinned to 3.12 (`>=3.12,<3.13`), managed by uv with a lockfile | Reproducible environments and a single interpreter version to support | Floating Python versions |
| D010 | 2026-11-07 | LF line endings everywhere, enforced by `.gitattributes` | File hashes (scenario manifests, golden files) must match on Windows and Linux | Git's default CRLF conversion on Windows |
| D011 | 2026-11-07 | Pre-commit runs ruff from the project environment (local hooks) | Hooks and CI use the same locked ruff version | A separately pinned `ruff-pre-commit` revision |
| D012 | 2026-11-07 | CI pins `astral-sh/setup-uv` to an exact release (`v10.3.3`) | That action publishes no floating major tag | `@v10`, which doesn't exist |
| D013 | 2026-11-07 | Determinism rules are enforced by tests (`tests/test_guardrails.py`) | Rules in a document get forgotten; a failing test doesn't | Code review only |
| D014 | 2026-11-07 | Truck routing uses a small hand-written Dijkstra that breaks ties by node ID (`sim/routing.py`); NetworkX waits for the M4 dependency graph | Every platform must pick exactly the same path among equally fast ones | NetworkX shortest paths (tie order depends on graph internals) |
| D015 | 2026-11-07 | Tick semantics: events at tick t apply before decisions at t. Each minute: hospitals burn, then trucks drive, load and unload, then statuses update. A delivery lands at the end of its fifth unloading minute, so it covers the next minute's burn | One fixed, documented order makes replays and tests exact | Other orderings (equally valid, but one has to be chosen) |
| D016 | 2026-11-07 | Scenario files are self-contained and versioned (`schema_version: 1`). The schema lives in `blackout.scenarios.schema`; event models live in `blackout.sim.events` | The engine's input contract has a single definition; the M5 generator writes the same format | Defining the schema inside the engine |
| D017 | 2026-11-07 | Already from M1, wake triggers come only from observable signals: fleet status, hospital telemetry, time-to-failure crossings, and a heartbeat 15 ticks after the last decision point. Hidden road events never wake anyone | Builds the M4 rule (D006) in from the start instead of retrofitting it | Truth-based triggers in M1, fixed in M4 |
| D018 | 2026-11-07 | Action fields carry their units (`amount_l`). A vehicle gets one movement action per decision point, and a rejected action doesn't use up its turn | Clear for people and LLMs; mistakes can be corrected within the same turn | Unitless `amount`; counting rejected attempts against the vehicle |
| D019 | 2026-11-07 | A breakdown freezes a truck where it is (mid-road allowed) and a repair resumes what it was doing. A stuck truck waits until it gets a new dispatch or reroute | Simple, predictable, and leaves recovery decisions to the contestant | Automatic rerouting by the engine |
| D020 | 2026-11-07 | Provisional: M1 hospital telemetry shows the true current burn rate | Keeps M1 simple. M4 may smooth or delay it so that T05 (burn spike) rewards reading staff reports | Hiding burn rate from M1 onwards |
| D021 | 2026-11-07 | A run stops early once no events are pending and every hospital has grid power | Nothing can change after that, so further minutes would only add cost | Always running to the horizon |
| D022 | 2026-11-07 | CI enforces an 85% branch-coverage floor. Golden run hashes in `tests/test_demo.py` pin the demo trajectories | Keeps the PRD's coverage target honest and catches unintended physics changes on both OSes | Coverage as a report only; no golden runs |
