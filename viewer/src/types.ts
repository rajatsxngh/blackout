// Shapes of the files that `blackout export-viewer` writes (src/blackout/logs/export_viewer.py).
// If you change one side, change the other and bump VIEWER_DATA_VERSION in both.

export const VIEWER_DATA_VERSION = 2; // 2: Decision.notes (M11)

// ---- manifest.json ------------------------------------------------------------------------

export interface ManifestRun {
  id: string; // run folder name, e.g. "rules__none__r0"
  contestant: string; // "rules", "solver", ...
  file: string; // path of the run file, relative to the data folder
  run_hash: string;
  ticks: number;
  decisions: number;
  critical_downtime_min: number | null;
  unmet_weighted_kw_min: number | null;
}

export interface ManifestScenario {
  id: string;
  note: string;
  template: string | null;
  level: number;
  tier: string | null;
  file: string;
  runs: ManifestRun[];
}

export interface Manifest {
  v: number;
  title: string;
  experiment: string;
  scenarios: ManifestScenario[];
}

// ---- scenarios/<id>.json: the static world ------------------------------------------------

export interface CityNode {
  id: string; // "E4" = Elm St & 4th Ave
  x: number; // column, west to east
  y: number; // row, north to south
  name: string;
}

export interface CityEdge {
  id: string; // "E3-E4"
  a: string;
  b: string;
  minutes: number;
  name: string;
}

export interface Fact {
  type: string;
  subject: string;
  value: string | number | null;
}

export interface Report {
  id: string;
  tick: number;
  channel: string;
  source: string;
  source_class: string; // verified / field / unverified
  text: string | null; // what the contestant read at L1+
  facts: Fact[]; // what the contestant read at L0
  claimed_facts: Fact[];
  // Ground truth, shown only behind the spoiler toggle:
  is_true: boolean;
  stale_of: string | null;
  corrects: string | null;
  cues: { kind: string; report: string | null; note: string }[];
  // An L3 injection: what it tries to make the contestant do, and in which ticks.
  injection: { kind: string; target: string; start: number; end: number; authority: string } | null;
}

export interface Scenario {
  v: number;
  id: string;
  description: string;
  template: string | null;
  level: number;
  tier: string | null;
  split: string;
  horizon: number;
  city: {
    width: number;
    height: number;
    nodes: CityNode[];
    edges: CityEdge[];
    landmarks: Record<string, string>;
  };
  hospitals: { id: string; name: string; node: string; tank_l: number; zone: string | null }[];
  depots: { id: string; name: string; node: string; stock_l: number }[];
  substations: { id: string; name: string; node: string; capacity_kw: number }[];
  zones: {
    id: string;
    name: string;
    nodes: string[];
    priority: "critical" | "essential" | "residential";
    demand_kw: number;
    primary: string;
    alternate: string | null;
  }[];
  trucks: { id: string; capacity_l: number }[];
  crews: { id: string }[];
  reports: Report[];
  events: Record<string, unknown>[]; // hidden scenario events (road blocks, breakdowns, ...)
}

// ---- runs/<scenario>/<run>.json: one run ---------------------------------------------------

export type VehicleState =
  | "idle"
  | "moving"
  | "stuck"
  | "loading"
  | "unloading"
  | "repairing"
  | "broken";

export interface VehicleFrame {
  at: string; // the node it's at, or the node it left if it's driving a road
  next: string | null; // the node ahead while driving, else null
  elapsed: number; // minutes driven on the current road
  route: string[]; // nodes still to visit
  state: VehicleState;
}

export interface TruckFrame extends VehicleFrame {
  load_l: number;
  task: string | null; // the depot or hospital it's heading to
}

export interface CrewFrame extends VehicleFrame {
  substation: string | null;
}

// One tick. Each list is in the scenario file's order (sorted by ID).
export interface Frame {
  t: number;
  trucks: TruckFrame[];
  crews: CrewFrame[];
  hospitals: {
    fuel_l: number;
    burn_lpm: number;
    status: "on_grid" | "on_generator" | "dark";
    dark_min: number;
  }[];
  depots: { stock_l: number }[];
  substations: {
    state: "online" | "failed" | "tripped" | "repairing";
    load_kw: number;
    work_left: number;
  }[];
  zones: { shed: boolean; fed_by: string | null }[];
  blocked: string[]; // road segments really blocked (truth)
  known_blocks: string[]; // blocks a vehicle has run into
  unmet_kw_min: number;
  unmet_weighted_kw_min: number;
}

export interface Estimate {
  value: string | number;
  as_of: number;
  source: string;
  confidence: number;
}

// What the contestant believed after a decision (blackout.info.belief.BeliefState.digest).
export interface Belief {
  tick: number;
  role: string;
  roads: Record<string, Estimate | null>;
  hospitals: Record<string, { fuel_l: Estimate | null; burn_lpm: Estimate | null }>;
  substations: Record<string, { state: Estimate | null; eta_minutes: Estimate | null }>;
  depots: Record<string, { stock_l: Estimate | null }>;
  pending_confirms: string[];
  confirms_left: number;
}

export interface ActionEntry {
  kind: string;
  raw: Record<string, unknown>; // exactly what the contestant sent, including its "reason"
  accepted: boolean;
  reason: string | null; // why it was rejected, if it was
  message: string;
  route: string[];
}

export interface Decision {
  i: number;
  t: number;
  role: string;
  triggers: { kind: string; subject: string; detail: string }[];
  actions: ActionEntry[];
  belief: Belief | null;
  // What the decision cost in model calls (LLM contestants only; null for the baselines).
  llm: { input_tokens: number; output_tokens: number; cost_usd: number; calls: number } | null;
  // The notes an LLM agent carried to its next turn (M11); null for the baselines.
  notes: string | null;
}

export interface Run {
  v: number;
  run_id: string;
  scenario: string;
  contestant: string;
  model: string | null;
  run_index: number;
  run_hash: string;
  end: { tick: number; reason: string; metrics: Record<string, unknown> };
  frames: Frame[];
  decisions: Decision[];
  reports: { t: number; id: string }[]; // when each report reached the contestant
  events: ({ t: number; kind: string } & Record<string, unknown>)[];
}
