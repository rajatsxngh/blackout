// Pure helpers: no React, no canvas. Everything here is unit-tested in logic.test.ts.

import type { Belief, Decision, Run, Scenario, VehicleFrame } from "./types";

// ---- positions ------------------------------------------------------------------------------

/** A point in grid units: x = column (street), y = row (avenue). */
export interface Point {
  x: number;
  y: number;
}

/** Lookup tables built once per scenario. */
export interface MapIndex {
  node: Map<string, Point>;
  edgeMinutes: Map<string, number>;
}

export function buildIndex(scenario: Scenario): MapIndex {
  const node = new Map<string, Point>();
  for (const n of scenario.city.nodes) node.set(n.id, { x: n.x, y: n.y });
  const edgeMinutes = new Map<string, number>();
  for (const e of scenario.city.edges) edgeMinutes.set(e.id, e.minutes);
  return { node, edgeMinutes };
}

/** Same rule as the engine: an edge ID joins its two node IDs in sorted order. */
export function edgeId(a: string, b: string): string {
  return a < b ? `${a}-${b}` : `${b}-${a}`;
}

/** Where a vehicle is at a tick: at a node, or part-way along the road it's driving. */
export function vehiclePoint(v: VehicleFrame, index: MapIndex): Point {
  const from = index.node.get(v.at) ?? { x: 0, y: 0 };
  if (v.next === null) return from;
  const to = index.node.get(v.next) ?? from;
  const minutes = index.edgeMinutes.get(edgeId(v.at, v.next)) ?? 1;
  return lerp(from, to, Math.min(1, v.elapsed / minutes));
}

export function lerp(a: Point, b: Point, f: number): Point {
  return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f };
}

// ---- time -----------------------------------------------------------------------------------

/** The last decision at or before tick ``t`` that recorded a belief, or null. */
export function beliefAt(run: Run, t: number): Belief | null {
  let found: Belief | null = null;
  for (const d of run.decisions) {
    if (d.t > t) break;
    if (d.belief !== null) found = d.belief;
  }
  return found;
}

/** The first value in a sorted list that is greater than ``t``, or null. */
export function nextAfter(sorted: number[], t: number): number | null {
  for (const x of sorted) if (x > t) return x;
  return null;
}

/** The last value in a sorted list that is less than ``t``, or null. */
export function previousBefore(sorted: number[], t: number): number | null {
  let found: number | null = null;
  for (const x of sorted) {
    if (x >= t) break;
    found = x;
  }
  return found;
}

/** Ticks at which the run had a decision point (sorted, no repeats). */
export function decisionTicks(run: Run): number[] {
  return [...new Set(run.decisions.map((d) => d.t))];
}

/** Ticks at which something happened: an event, a report, or a decision with actions. */
export function eventTicks(run: Run): number[] {
  const ticks = new Set<number>();
  for (const e of run.events) ticks.add(e.t);
  for (const r of run.reports) ticks.add(r.t);
  for (const d of run.decisions) if (d.actions.length > 0) ticks.add(d.t);
  return [...ticks].sort((a, b) => a - b);
}

// ---- side by side ---------------------------------------------------------------------------

/** An action as a comparable string: what it does, without the free-text reason. */
export function actionKey(raw: Record<string, unknown>): string {
  const keys = Object.keys(raw)
    .filter((k) => k !== "reason")
    .sort();
  return JSON.stringify(keys.map((k) => [k, raw[k]]));
}

function decisionKey(d: Decision): string {
  return `${d.t}|${d.actions.map((a) => actionKey(a.raw)).join(";")}`;
}

export interface Divergence {
  tick: number; // when the two runs first did something different
  indexA: number | null; // that decision in run A (null if A had none there)
  indexB: number | null;
}

/**
 * The first decision where two runs of one scenario differ: a different tick, or different
 * actions (reasons ignored). Null if every decision matches.
 */
export function firstDivergence(a: Run, b: Run): Divergence | null {
  const n = Math.max(a.decisions.length, b.decisions.length);
  for (let i = 0; i < n; i++) {
    const da = a.decisions[i];
    const db = b.decisions[i];
    if (da && db && decisionKey(da) === decisionKey(db)) continue;
    if (!da && db) return { tick: db.t, indexA: null, indexB: db.i };
    if (da && !db) return { tick: da.t, indexA: da.i, indexB: null };
    if (da && db) {
      // Different ticks: the earlier one is where they parted.
      if (da.t !== db.t) {
        return da.t < db.t
          ? { tick: da.t, indexA: da.i, indexB: null }
          : { tick: db.t, indexA: null, indexB: db.i };
      }
      return { tick: da.t, indexA: da.i, indexB: db.i };
    }
  }
  return null;
}

// ---- decisions ------------------------------------------------------------------------------

/** IDs an action refers to (vehicle, hospital, depot, zone, substation), for highlighting. */
export function entitiesOf(raw: Record<string, unknown>): string[] {
  // The one acting (truck, vehicle, crew) first, then what it acts on.
  const first = ["truck_id", "vehicle_id", "crew_id"];
  const keys = Object.keys(raw)
    .filter((k) => k.endsWith("_id") && typeof raw[k] === "string")
    .sort((x, y) => Number(first.includes(y)) - Number(first.includes(x)));
  return keys.map((k) => raw[k] as string);
}

/** A short one-line summary of an action, e.g. "dispatch T2 -> H1 (400 L)". */
export function describeAction(raw: Record<string, unknown>): string {
  const kind = String(raw.kind ?? "?");
  const ids = entitiesOf(raw);
  const amount = typeof raw.amount_l === "number" ? ` (${raw.amount_l} L)` : "";
  if (ids.length >= 2) return `${kind} ${ids[0]} → ${ids.slice(1).join(", ")}${amount}`;
  return `${kind} ${ids.join(" ")}${amount}`.trim();
}

/** Minutes of generator fuel left, or null if the hospital has grid power. */
export function minutesLeft(fuel: number, burn: number, onGrid: boolean): number | null {
  if (onGrid) return null;
  return burn > 0 ? Math.floor(fuel / burn) : Infinity;
}

// ---- deep links -----------------------------------------------------------------------------

/** ?s=<scenario>&a=<run>&b=<run>&t=<tick> (PRD 8.13). Every part is optional. */
export interface Link {
  s: string | null;
  a: string | null;
  b: string | null;
  t: number | null;
}

export function parseLink(search: string): Link {
  const p = new URLSearchParams(search);
  const t = p.get("t");
  const tick = t !== null && /^\d+$/.test(t) ? Number(t) : null;
  return { s: p.get("s"), a: p.get("a"), b: p.get("b"), t: tick };
}

export function buildLink(link: Link): string {
  const p = new URLSearchParams();
  if (link.s) p.set("s", link.s);
  if (link.a) p.set("a", link.a);
  if (link.b) p.set("b", link.b);
  if (link.t !== null) p.set("t", String(link.t));
  const text = p.toString();
  return text ? `?${text}` : "";
}
