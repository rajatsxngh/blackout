// Drawing one map on a Canvas 2D context. Called up to 60 times a second while playing, so it
// only draws: everything it needs comes in through `MapScene`.

import { lerp, minutesLeft, vehiclePoint, type MapIndex, type Point } from "./logic";
import type { Palette } from "./palette";
import type { Belief, Frame, Scenario, VehicleFrame } from "./types";

export type View = "truth" | "belief";

export interface MapScene {
  scenario: Scenario;
  index: MapIndex;
  frame: Frame; // the tick we're in
  next: Frame; // the tick after it (same as `frame` at the end), for smooth movement
  fraction: number; // how far we are from `frame` to `next`, 0..1
  view: View;
  belief: Belief | null; // what the contestant believed at this tick (for the belief view)
  highlight: Set<string>; // entity IDs to ring (from a clicked decision)
  palette: Palette;
  width: number; // CSS pixels
  height: number;
  now: number; // milliseconds, for flashing
}

/** Minutes of backup a full countdown bar stands for. */
const BAR_MINUTES = 120;
/** Below this many minutes a hospital's bar flashes (PRD 8.13). */
const LOW_MINUTES = 15;

export function drawMap(ctx: CanvasRenderingContext2D, scene: MapScene): void {
  const { scenario, palette: pal, width, height } = scene;
  const { width: cols, height: rows } = scenario.city;

  // Grid geometry: one "cell" is the distance between neighboring intersections. Leave about
  // half a cell around the grid for the zone squares, icons and labels at the edges.
  const pad = 0.6;
  const cell = Math.min(width / (cols - 1 + 2 * pad), (height - 16) / (rows - 1 + 2 * pad));
  const left = (width - cell * (cols - 1)) / 2;
  const top = (height - cell * (rows - 1)) / 2;
  const px = (p: Point): Point => ({ x: left + p.x * cell, y: top + p.y * cell });
  const nodePx = (id: string): Point => px(scene.index.node.get(id) ?? { x: 0, y: 0 });

  ctx.fillStyle = pal.background;
  ctx.fillRect(0, 0, width, height);

  drawZones(ctx, scene, cell, nodePx);
  drawRoads(ctx, scene, cell, nodePx);
  drawRoutes(ctx, scene, cell, px, nodePx);
  drawFacilities(ctx, scene, cell, nodePx);
  drawVehicles(ctx, scene, cell, px);
}

// ---- zones: tinted by priority, dark when unpowered, hatched when shed ----------------------

function drawZones(
  ctx: CanvasRenderingContext2D,
  scene: MapScene,
  cell: number,
  nodePx: (id: string) => Point,
): void {
  const pal = scene.palette;
  const tint = {
    critical: pal.zoneCritical,
    essential: pal.zoneEssential,
    residential: pal.zoneResidential,
  };
  scene.scenario.zones.forEach((zone, i) => {
    const state = scene.frame.zones[i];
    for (const node of zone.nodes) {
      const c = nodePx(node);
      const x = c.x - cell / 2;
      const y = c.y - cell / 2;
      ctx.fillStyle = tint[zone.priority];
      ctx.fillRect(x, y, cell, cell);
      if (state.fed_by === null) {
        ctx.fillStyle = pal.unpowered;
        ctx.fillRect(x, y, cell, cell);
      }
      if (state.shed) hatch(ctx, x, y, cell, pal.hatch);
    }
    // Label the zone once, at its first intersection's corner.
    const first = nodePx(zone.nodes[0]);
    ctx.fillStyle = pal.muted;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    const status = state.shed ? " shed" : state.fed_by === null ? " no power" : "";
    ctx.fillText(`${zone.id}${status}`, first.x - cell / 2 + 3, first.y - cell / 2 + 3);
  });
}

function hatch(ctx: CanvasRenderingContext2D, x: number, y: number, size: number, color: string) {
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, size, size);
  ctx.clip();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let d = -size; d < size; d += 8) {
    ctx.moveTo(x + d, y + size);
    ctx.lineTo(x + d + size, y);
  }
  ctx.stroke();
  ctx.restore();
}

// ---- roads: blocked ones as dashes, from the truth or from the contestant's belief ----------

function drawRoads(
  ctx: CanvasRenderingContext2D,
  scene: MapScene,
  cell: number,
  nodePx: (id: string) => Point,
): void {
  const pal = scene.palette;
  const blocked = new Set<string>();
  if (scene.view === "truth") {
    for (const e of scene.frame.blocked) blocked.add(e);
  } else if (scene.belief) {
    for (const [edge, estimate] of Object.entries(scene.belief.roads)) {
      if (estimate && estimate.value === "blocked") blocked.add(edge);
    }
  }
  ctx.lineCap = "round";
  for (const edge of scene.scenario.city.edges) {
    const a = nodePx(edge.a);
    const b = nodePx(edge.b);
    ctx.strokeStyle = pal.road;
    ctx.lineWidth = Math.max(2, cell * 0.06);
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    // Travel minutes, small, in the middle of the road.
    ctx.fillStyle = pal.roadLabel;
    ctx.font = "9px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const mid = lerp(a, b, 0.5);
    const off = a.x === b.x ? { x: 7, y: 0 } : { x: 0, y: -7 };
    ctx.fillText(String(edge.minutes), mid.x + off.x, mid.y + off.y);
  }
  for (const id of blocked) {
    const [na, nb] = id.split("-");
    const a = nodePx(na);
    const b = nodePx(nb);
    ctx.strokeStyle = scene.view === "truth" ? pal.blocked : pal.believedBlocked;
    ctx.lineWidth = Math.max(3, cell * 0.08);
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }
  ctx.setLineDash([]);
}

// ---- planned routes: a thin line ahead of each moving vehicle -------------------------------

function drawRoutes(
  ctx: CanvasRenderingContext2D,
  scene: MapScene,
  cell: number,
  px: (p: Point) => Point,
  nodePx: (id: string) => Point,
): void {
  const vehicles: VehicleFrame[] = [...scene.frame.trucks, ...scene.frame.crews];
  ctx.lineWidth = Math.max(1.5, cell * 0.025);
  ctx.globalAlpha = 0.55;
  for (const v of vehicles) {
    if (v.route.length === 0) continue;
    ctx.strokeStyle = scene.palette.vehicle[v.state];
    const start = px(vehiclePoint(v, scene.index));
    ctx.beginPath();
    ctx.moveTo(start.x, start.y);
    for (const node of v.route) {
      const p = nodePx(node);
      ctx.lineTo(p.x, p.y);
    }
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

// ---- facilities: hospitals, substations, depots ---------------------------------------------

// Facilities sharing an intersection sit in different corners of it.
const OFFSET = { hospital: { x: -0.24, y: -0.24 }, substation: { x: 0.24, y: -0.24 }, depot: { x: -0.24, y: 0.24 } };

function drawFacilities(
  ctx: CanvasRenderingContext2D,
  scene: MapScene,
  cell: number,
  nodePx: (id: string) => Point,
): void {
  const { scenario, frame, palette: pal, belief, view } = scene;
  const size = Math.min(26, Math.max(12, cell * 0.22));
  const at = (node: string, o: Point): Point => {
    const c = nodePx(node);
    return { x: c.x + o.x * cell, y: c.y + o.y * cell };
  };
  const flashOn = Math.floor(scene.now / 400) % 2 === 0;

  // Substations: a square colored by state (believed state in the belief view).
  scenario.substations.forEach((s, i) => {
    const p = at(s.node, OFFSET.substation);
    const truth = frame.substations[i];
    let state = truth.state;
    if (view === "belief") {
      const believed = belief?.substations[s.id]?.state?.value;
      if (typeof believed === "string" && believed in pal.substation) state = believed as typeof state;
    }
    ctx.fillStyle = pal.substation[state];
    ctx.strokeStyle = pal.text;
    ctx.lineWidth = 1;
    ctx.fillRect(p.x - size / 2, p.y - size / 2, size, size);
    ctx.strokeRect(p.x - size / 2, p.y - size / 2, size, size);
    if (truth.load_kw > s.capacity_kw && view === "truth") {
      ctx.fillStyle = pal.text;
      ctx.font = `bold ${Math.round(size * 0.8)}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("!", p.x, p.y + 1);
    }
    label(ctx, s.id, p.x, p.y + size / 2 + 2, pal.text);
    ring(ctx, scene, s.id, p, size);
  });

  // Hospitals: a cross icon (dark when the hospital is dark) and a countdown bar.
  scenario.hospitals.forEach((h, i) => {
    const p = at(h.node, OFFSET.hospital);
    const truth = frame.hospitals[i];
    const dark = truth.status === "dark";
    let fuel = truth.fuel_l;
    let burn = truth.burn_lpm;
    if (view === "belief") {
      const b = belief?.hospitals[h.id];
      if (typeof b?.fuel_l?.value === "number") fuel = b.fuel_l.value;
      if (typeof b?.burn_lpm?.value === "number") burn = b.burn_lpm.value;
    }
    ctx.fillStyle = dark ? pal.hospitalDark : pal.hospital;
    ctx.strokeStyle = pal.text;
    ctx.lineWidth = 1.5;
    roundRect(ctx, p.x - size / 2, p.y - size / 2, size, size, 3);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = dark ? pal.muted : pal.barLow;
    const arm = size * 0.18;
    ctx.fillRect(p.x - arm / 2, p.y - size * 0.32, arm, size * 0.64);
    ctx.fillRect(p.x - size * 0.32, p.y - arm / 2, size * 0.64, arm);

    // Countdown bar under the icon: minutes of backup left.
    const left = minutesLeft(fuel, burn, truth.status === "on_grid");
    const barW = Math.min(64, Math.max(30, cell * 0.5));
    const barH = 5;
    const bx = p.x - barW / 2;
    const by = p.y + size / 2 + 3;
    ctx.fillStyle = pal.barTrack;
    ctx.fillRect(bx, by, barW, barH);
    let text: string;
    if (left === null) {
      ctx.fillStyle = pal.barOk;
      ctx.fillRect(bx, by, barW, barH);
      text = "grid";
    } else {
      const low = left < LOW_MINUTES;
      const share = Math.min(1, left / BAR_MINUTES);
      if (!low || flashOn) {
        ctx.fillStyle = low ? pal.barLow : pal.barOk;
        ctx.fillRect(bx, by, barW * share, barH);
      }
      text = dark ? "DARK" : `${left === Infinity ? "∞" : left}m`;
    }
    label(ctx, `${h.id} ${text}`, p.x, by + barH + 1, pal.text);
    ring(ctx, scene, h.id, p, size);
  });

  // Depots: a triangle and a stock bar (share of the starting stock).
  scenario.depots.forEach((d, i) => {
    const p = at(d.node, OFFSET.depot);
    let stock = frame.depots[i].stock_l;
    if (view === "belief") {
      const believed = belief?.depots[d.id]?.stock_l?.value;
      if (typeof believed === "number") stock = believed;
    }
    ctx.fillStyle = pal.depot;
    ctx.strokeStyle = pal.text;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(p.x, p.y - size / 2);
    ctx.lineTo(p.x + size / 2, p.y + size / 2);
    ctx.lineTo(p.x - size / 2, p.y + size / 2);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    const barW = Math.min(56, Math.max(26, cell * 0.4));
    const bx = p.x - barW / 2;
    const by = p.y + size / 2 + 3;
    ctx.fillStyle = pal.barTrack;
    ctx.fillRect(bx, by, barW, 4);
    ctx.fillStyle = pal.depot;
    ctx.fillRect(bx, by, barW * Math.min(1, stock / Math.max(1, d.stock_l)), 4);
    label(ctx, `${d.id} ${stock} L`, p.x, by + 5, pal.text);
    ring(ctx, scene, d.id, p, size);
  });
}

// ---- trucks and crews: moving smoothly between ticks ----------------------------------------

function drawVehicles(
  ctx: CanvasRenderingContext2D,
  scene: MapScene,
  cell: number,
  px: (p: Point) => Point,
): void {
  const { scenario, frame, next, fraction, palette: pal } = scene;
  const r = Math.min(11, Math.max(6, cell * 0.1));
  const where = (now: VehicleFrame, then: VehicleFrame | undefined) =>
    px(lerp(vehiclePoint(now, scene.index), vehiclePoint(then ?? now, scene.index), fraction));

  // Spread vehicles that stand on the same spot, so none hides another.
  const taken = new Map<string, number>();
  const spread = (p: Point): Point => {
    const key = `${Math.round(p.x)},${Math.round(p.y)}`;
    const n = taken.get(key) ?? 0;
    taken.set(key, n + 1);
    return n === 0 ? p : { x: p.x + r * 1.3 * Math.cos(n * 2.1), y: p.y + r * 1.3 * Math.sin(n * 2.1) };
  };

  // Trucks: a circle in the color of what they're doing, with a pie showing how full they are.
  scenario.trucks.forEach((t, i) => {
    const v = frame.trucks[i];
    const p = spread(where(v, next.trucks[i]));
    ctx.fillStyle = pal.vehicle[v.state];
    ctx.strokeStyle = pal.background;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    const share = v.load_l / Math.max(1, t.capacity_l);
    if (share > 0) {
      ctx.fillStyle = "#ffffff";
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.arc(p.x, p.y, r * 0.55, -Math.PI / 2, -Math.PI / 2 + share * Math.PI * 2);
      ctx.closePath();
      ctx.fill();
    }
    label(ctx, t.id, p.x + r + 8, p.y - 5, pal.text);
    ring(ctx, scene, t.id, p, r * 2);
  });

  // Crews: a diamond.
  scenario.crews.forEach((c, i) => {
    const v = frame.crews[i];
    const p = spread(where(v, next.crews[i]));
    ctx.fillStyle = v.state === "repairing" ? pal.substation.repairing : pal.crew;
    ctx.strokeStyle = pal.text;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(p.x, p.y - r);
    ctx.lineTo(p.x + r, p.y);
    ctx.lineTo(p.x, p.y + r);
    ctx.lineTo(p.x - r, p.y);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    label(ctx, c.id, p.x + r + 8, p.y - 5, pal.text);
    ring(ctx, scene, c.id, p, r * 2);
  });
}

// ---- small helpers ----------------------------------------------------------------------------

function label(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, color: string) {
  ctx.fillStyle = color;
  ctx.font = "10px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillText(text, x, y);
}

/** A ring around an entity that a clicked decision refers to. */
function ring(ctx: CanvasRenderingContext2D, scene: MapScene, id: string, p: Point, size: number) {
  if (!scene.highlight.has(id)) return;
  ctx.strokeStyle = scene.palette.highlight;
  ctx.lineWidth = 2;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.arc(p.x, p.y, size * 0.9 + 4, 0, Math.PI * 2);
  ctx.stroke();
  ctx.setLineDash([]);
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
