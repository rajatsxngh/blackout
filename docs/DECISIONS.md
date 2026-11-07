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
