# configs/

YAML configuration, validated by Pydantic when it's loaded. Each file arrives with the milestone that needs it (docs/PRD.md §12):

| File | Milestone | Contents |
|---|---|---|
| `city/default.yaml` | M5 (done) | The scenario generator's grid size, entity counts, parameter ranges and per-template settings (hand-made scenarios carry their own values) |
| `matrices/*.yaml` | M6 | Experiment matrices for `blackout batch` |
| `models.yaml` | M10 | Model IDs, effort levels, and a dated price table |
| `budget.yaml` | M10 | Spend caps per experiment and overall |
