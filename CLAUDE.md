# Blackout — working agreement

Research sandbox: Claude agents vs a rules script vs OR-Tools in a simulated city blackout. Not emergency software.

- Source of truth: docs/PRD.md. Build milestone by milestone (§12). Don't start a milestone before its dependencies are done; update the status table in §12 when one completes.
- Python 3.12 + uv. Checks: `uv run pytest`, `uv run ruff check .`, `uv run pyright`.
- Determinism: no randomness in the engine (seeded RNG only in the scenario generator); blake2b seeds, never `hash()`; integer units; sorted iteration; no wall clock in the sim or solver; Windows spawn guard (`if __name__ == "__main__":`) on entry points.
- Contestants never read ground truth (except the reference contestant). Wake triggers come from observations.
- Never open, edit or tune against `data/phrasing/test/` or `scenarios/test/`. The test bank is human-written.
- LLM code: official `anthropic` SDK; read the claude-api skill's Python docs before writing it; no network calls in tests (recorded fixtures); every batch goes through the budget guard.
- Log decisions in docs/DECISIONS.md.
- Golden run hashes in tests/test_demo.py change only when physics changes on purpose: update them, and say why in docs/DECISIONS.md.
- Handy: `uv run blackout run -s hello_blackout -c scripted --render ascii` (or `-s cascade`) shows a demo and logs it to runs/adhoc; `uv run blackout replay <run dir>` verifies a log; `uv run blackout score runs/adhoc` scores every logged run; `uv run blackout gen --suite dev-v0` regenerates scenarios/dev-v0 (`--check` verifies it, `--sample 20` prints L1 reports); `HYPOTHESIS_PROFILE=thorough uv run pytest tests/test_properties.py tests/test_replay.py` runs a deep property check; `uv run blackout batch --matrix configs/matrices/dev_baselines.yaml [--resume]` runs do-nothing and Rules over dev-v0 and writes results/dev_baselines/ (tables and parse F1).
- Run logs must stay deterministic (docs/DECISIONS.md D033): never put wall-clock time or machine details in run.jsonl.gz, only in summary.json.

Current milestone: M0–M5 are done (CI green). M6 is done locally; it still needs its CI run (push to GitHub). Next: M7.
