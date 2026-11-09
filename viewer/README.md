# Blackout replay viewer

A static web page that plays back Blackout runs (docs/PRD.md §8.13). No backend: it reads JSON
files that `blackout export-viewer` writes into `public/data/`.

## Run it locally

From the repository root:

```bash
uv run blackout export-viewer <experiment> --select configs/viewer/preview.yaml
cd viewer
npm ci
npm run dev        # http://localhost:5173
```

`<experiment>` is a folder in `runs/` (made by `blackout batch` or `blackout run --out`). The
selection file lists the scenarios and run folders to export; see `configs/viewer/preview.yaml`.
The README at the repository root shows how to make the preview's runs.

| Command | What it does |
|---|---|
| `npm run dev` | Development server with hot reload |
| `npm run lint` | TypeScript type check (strict) |
| `npm test` | Unit tests for the pure logic (`src/logic.test.ts`, Vitest) |
| `npm run build` | Type check, then a static build in `dist/` (relative URLs, so it works on GitHub Pages) |

## How it's built

| File | What it does |
|---|---|
| `src/types.ts` | The shapes of the exported files; mirrors `src/blackout/logs/export_viewer.py` |
| `src/data.ts` | Fetches and caches `manifest.json`, scenario files and run files |
| `src/logic.ts` | Pure helpers: vehicle positions, belief at a tick, divergence, deep links |
| `src/draw.ts` | Draws one map on a canvas (zones, roads, routes, facilities, vehicles) |
| `src/palette.ts` | Map colors for light and dark themes (Okabe-Ito, colorblind-safe) |
| `src/useClock.ts` | The playback clock (fractional ticks, so trucks move smoothly) |
| `src/App.tsx` | Run picker, deep links, keyboard, and the `Player` that wires everything up |
| `src/components/` | The map, timeline, legend, and the decisions, reports and metrics panels |

A frame is the world after a tick's events and after any decision made at that tick. Between two
frames the map moves vehicles in a straight line, so playback stays smooth at any speed.

**Divergence** (side by side): the first decision where the two runs differ, either in when they
decided or in what they did (the free-text reason doesn't count).

**Truth vs belief:** "what it believed" draws roads, substation states, hospital fuel and depot
stock from the contestant's belief after its latest decision (logged since M4). Contestants
without a belief (do-nothing, scripted) show nothing believed.

## Changing the data format

Change `export_viewer.py` and `src/types.ts` together and bump `VIEWER_DATA_VERSION` in both;
the viewer refuses files with another version.
