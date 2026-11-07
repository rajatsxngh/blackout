# configs/

YAML configuration, validated by Pydantic when it's loaded. Each file arrives with the milestone that needs it (docs/PRD.md §12):

| File | Milestone | Contents |
|---|---|---|
| `city/default.yaml` | M1–M2 | Grid size, entity counts and parameter ranges |
| `matrices/*.yaml` | M6 | Experiment matrices for `blackout batch` |
| `models.yaml` | M10 | Model IDs, effort levels, and a dated price table |
| `budget.yaml` | M10 | Spend caps per experiment and overall |
