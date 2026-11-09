# configs/

YAML configuration, validated by Pydantic when it's loaded. Each file arrives with the milestone that needs it (docs/PRD.md §12):

| File | Milestone | Contents |
|---|---|---|
| `city/default.yaml` | M5 (done) | The scenario generator's grid size, entity counts, parameter ranges and per-template settings (hand-made scenarios carry their own values) |
| `matrices/*.yaml` | M6 (done) | Experiment matrices for `blackout batch`; `dev_baselines.yaml` runs do-nothing and Rules over dev-v0 |
| `models.yaml` | M10 (done) | Model IDs (dev: Haiku 5.5; final: Opus 5.5 and Haiku 5.5), explicit effort per purpose, server-side fallback per model, and the dated price table |
| `budget.yaml` | M10 (done) | Spend caps: global, per bucket of experiments (dev, v1_test, team_stress, reserve) and per run, plus the 5x-median abort and the infra-fault re-run threshold |
| `matrices/dev_c4_haiku.yaml`, `matrices/m10_c4_fixtures.yaml` | M10 (done) | C4 vs C2 on dev with live Haiku (waits for the locked test bank), and C4 on synthetic fixtures (no API calls) |
