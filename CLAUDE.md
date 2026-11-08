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
- Handy: `uv run blackout run -s hello_blackout -c scripted --render ascii` (or `-s cascade`) shows a demo and logs it to runs/adhoc; `uv run blackout replay <run dir>` verifies a log; `uv run blackout score runs/adhoc` scores every logged run; `HYPOTHESIS_PROFILE=thorough uv run pytest tests/test_properties.py tests/test_replay.py` runs a deep property check; `uv run blackout run -s trap_assignment -c solver` (or `-c rules`, or `-s trap_depot`) shows where planning beats greedy dispatch; the solver's library contract is `blackout.controllers.solver.solve_plan` (docs/DECISIONS.md D061); `uv run blackout verify --suite dev` re-runs the frozen M8 dev suite and checks it's valid on this OS, and `uv run blackout gen --suite dev --check` regenerates it byte for byte (Windows only: CP-SAT is deterministic per OS, not across OSes, D084) (dev-v0 is frozen from M5 and can't be regenerated, D071); `uv run blackout batch --matrix configs/matrices/dev_baselines.yaml` runs the four baselines over dev (Rules vs Solver vs Reference by template, level and tier in results/dev_baselines/results.md); `uv run blackout prereg-hashes` prints the block the test guard checks in docs/PREREGISTRATION.md (M12). Scenario calibration uses do-nothing, Rules and the reference only, never the Solver (D072).
- Run logs must stay deterministic (docs/DECISIONS.md D033): never put wall-clock time or machine details in run.jsonl.gz, only in summary.json.

Current milestone: M0–M7 are done (CI green). M8 is done except the test-suite freeze: `data/phrasing/test/` must first be written by a human (docs/TEST_BANK_GUIDE.md), checked with `uv run blackout bank check`, locked with `uv run blackout bank lock` and committed; then `uv run blackout gen --suite test` generates scenarios/test. Never write, read or print the test bank (no `--sample` on test). Next after that: M9.
