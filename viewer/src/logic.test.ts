import { describe, expect, it } from "vitest";
import {
  actionKey,
  beliefAt,
  buildLink,
  describeAction,
  edgeId,
  entitiesOf,
  eventTicks,
  firstDivergence,
  minutesLeft,
  nextAfter,
  parseLink,
  previousBefore,
  vehiclePoint,
  type MapIndex,
} from "./logic";
import type { Belief, Decision, Run, VehicleFrame } from "./types";

const index: MapIndex = {
  node: new Map([
    ["A1", { x: 0, y: 0 }],
    ["B1", { x: 1, y: 0 }],
  ]),
  edgeMinutes: new Map([["A1-B1", 4]]),
};

const vehicle = (over: Partial<VehicleFrame>): VehicleFrame => ({
  at: "A1",
  next: null,
  elapsed: 0,
  route: [],
  state: "idle",
  ...over,
});

const decision = (i: number, t: number, raws: Record<string, unknown>[], belief: Belief | null = null): Decision => ({
  i,
  t,
  role: "all",
  triggers: [],
  actions: raws.map((raw) => ({ kind: String(raw.kind), raw, accepted: true, reason: null, message: "", route: [] })),
  belief,
  llm: null,
});

const run = (decisions: Decision[], extra: Partial<Run> = {}): Run => ({
  v: 1,
  run_id: "s/x",
  scenario: "s",
  contestant: "x",
  model: null,
  run_index: 0,
  run_hash: "",
  end: { tick: 10, reason: "stable", metrics: {} },
  frames: [],
  decisions,
  reports: [],
  events: [],
  ...extra,
});

describe("positions", () => {
  it("puts a parked vehicle on its node", () => {
    expect(vehiclePoint(vehicle({}), index)).toEqual({ x: 0, y: 0 });
  });
  it("puts a driving vehicle part-way along its road", () => {
    expect(vehiclePoint(vehicle({ next: "B1", elapsed: 1 }), index)).toEqual({ x: 0.25, y: 0 });
    expect(vehiclePoint(vehicle({ at: "B1", next: "A1", elapsed: 3 }), index)).toEqual({ x: 0.25, y: 0 });
  });
  it("names edges like the engine does", () => {
    expect(edgeId("E4", "E3")).toBe("E3-E4");
    expect(edgeId("C4", "E4")).toBe("C4-E4");
  });
});

describe("time helpers", () => {
  it("finds the next and previous ticks", () => {
    expect(nextAfter([0, 5, 9], 5)).toBe(9);
    expect(nextAfter([0, 5, 9], 9)).toBeNull();
    expect(previousBefore([0, 5, 9], 5)).toBe(0);
    expect(previousBefore([0, 5, 9], 0)).toBeNull();
  });
  it("takes the latest belief at or before a tick", () => {
    const b = (tick: number) => ({ tick }) as Belief;
    const r = run([decision(0, 0, [], b(0)), decision(1, 4, []), decision(2, 6, [], b(6))]);
    expect(beliefAt(r, 5)?.tick).toBe(0);
    expect(beliefAt(r, 6)?.tick).toBe(6);
  });
  it("collects event ticks from events, reports and actions", () => {
    const r = run([decision(0, 0, []), decision(1, 3, [{ kind: "shed_load", zone_id: "Z1" }])], {
      events: [{ t: 7, kind: "road_block" }],
      reports: [{ t: 2, id: "R01" }],
    });
    expect(eventTicks(r)).toEqual([2, 3, 7]);
  });
});

describe("divergence", () => {
  const go = (truck: string, reason: string) => ({ kind: "dispatch_truck", truck_id: truck, target_id: "H1", reason });

  it("ignores the free-text reason", () => {
    expect(actionKey(go("T1", "a"))).toBe(actionKey(go("T1", "b")));
  });
  it("is null when every decision matches", () => {
    const a = run([decision(0, 0, [go("T1", "x")])]);
    const b = run([decision(0, 0, [go("T1", "y")])]);
    expect(firstDivergence(a, b)).toBeNull();
  });
  it("finds the first different action", () => {
    const a = run([decision(0, 0, []), decision(1, 2, [go("T1", "")])]);
    const b = run([decision(0, 0, []), decision(1, 2, [go("T2", "")])]);
    expect(firstDivergence(a, b)).toEqual({ tick: 2, indexA: 1, indexB: 1 });
  });
  it("takes the earlier tick when decision points drift apart", () => {
    const a = run([decision(0, 0, []), decision(1, 5, [])]);
    const b = run([decision(0, 0, []), decision(1, 3, [])]);
    expect(firstDivergence(a, b)).toEqual({ tick: 3, indexA: null, indexB: 1 });
  });
  it("notices when one run has more decisions", () => {
    const a = run([decision(0, 0, [])]);
    const b = run([decision(0, 0, []), decision(1, 8, [])]);
    expect(firstDivergence(a, b)).toEqual({ tick: 8, indexA: null, indexB: 1 });
  });
});

describe("actions and hospitals", () => {
  it("lists the entities an action names", () => {
    expect(entitiesOf({ kind: "assign_crew", crew_id: "C1", substation_id: "S2", reason: "x" })).toEqual(["C1", "S2"]);
  });
  it("describes an action in one line", () => {
    expect(describeAction({ amount_l: 400, kind: "dispatch_truck", target_id: "H1", truck_id: "T2" })).toBe(
      "dispatch_truck T2 → H1 (400 L)",
    );
    expect(describeAction({ kind: "shed_load", zone_id: "Z3" })).toBe("shed_load Z3");
  });
  it("counts minutes of fuel left", () => {
    expect(minutesLeft(26, 2, false)).toBe(13);
    expect(minutesLeft(26, 2, true)).toBeNull();
  });
});

describe("deep links", () => {
  it("round-trips", () => {
    const link = { s: "trap_depot", a: "rules__none__r0", b: "solver__none__r0", t: 17 };
    expect(parseLink(buildLink(link))).toEqual(link);
  });
  it("ignores a bad tick and missing parts", () => {
    expect(parseLink("?s=x&t=abc")).toEqual({ s: "x", a: null, b: null, t: null });
    expect(buildLink({ s: null, a: null, b: null, t: null })).toBe("");
  });
});
