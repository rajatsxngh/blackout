// What the shapes and colors on the map mean.

import type { Palette } from "../palette";

export function Legend({ palette: p }: { palette: Palette }) {
  const swatch = (color: string, shape: "dot" | "square" | "diamond" | "tri" = "dot") => (
    <span className={`swatch ${shape}`} style={{ background: color }} />
  );
  return (
    <details className="legend">
      <summary>Legend</summary>
      <ul>
        <li>{swatch(p.vehicle.moving)} truck moving (white pie = fuel aboard)</li>
        <li>{swatch(p.vehicle.loading)} loading · {swatch(p.vehicle.unloading)} unloading</li>
        <li>{swatch(p.vehicle.idle)} idle · {swatch(p.vehicle.stuck)} stuck · {swatch(p.vehicle.broken)} broken</li>
        <li>{swatch(p.crew, "diamond")} repair crew</li>
        <li>
          {swatch(p.substation.online, "square")} substation online · {swatch(p.substation.failed, "square")} failed ·{" "}
          {swatch(p.substation.tripped, "square")} tripped · {swatch(p.substation.repairing, "square")} repairing · ! overloaded
        </li>
        <li>{swatch(p.hospital, "square")} hospital, bar = minutes of backup fuel (flashes under 15)</li>
        <li>{swatch(p.depot, "tri")} fuel depot, bar = stock left</li>
        <li>
          <span className="dash" style={{ borderColor: p.blocked }} /> blocked road (truth) ·{" "}
          <span className="dash" style={{ borderColor: p.believedBlocked }} /> believed blocked
        </li>
        <li>
          <span className="swatch square" style={{ background: p.zoneCritical }} /> critical ·{" "}
          <span className="swatch square" style={{ background: p.zoneEssential }} /> essential ·{" "}
          <span className="swatch square" style={{ background: p.zoneResidential }} /> residential zone; darkened = no
          power; hatched = shed
        </li>
        <li className="muted">Numbers on roads are travel minutes. Thin lines show each vehicle's route ahead.</li>
      </ul>
    </details>
  );
}
