// Colors for the map. Built on the Okabe-Ito palette, which stays distinguishable for the
// common kinds of color blindness; anything that matters also differs by shape or pattern
// (hatching for shed zones, dashes for blocked roads, icons for facilities).

export type Theme = "light" | "dark";

export interface Palette {
  background: string;
  road: string;
  roadLabel: string;
  text: string;
  muted: string;
  zoneCritical: string;
  zoneEssential: string;
  zoneResidential: string;
  unpowered: string; // overlay on a zone without power
  hatch: string; // hatching on a shed zone
  blocked: string; // blocked road (truth)
  believedBlocked: string; // road the contestant believed blocked
  highlight: string;
  substation: Record<"online" | "failed" | "tripped" | "repairing", string>;
  vehicle: Record<"idle" | "moving" | "stuck" | "loading" | "unloading" | "repairing" | "broken", string>;
  crew: string;
  hospital: string;
  hospitalDark: string;
  barOk: string;
  barLow: string;
  barTrack: string;
  depot: string;
}

const okabeIto = {
  orange: "#E69F00",
  sky: "#56B4E9",
  green: "#009E73",
  yellow: "#F0E442",
  blue: "#0072B2",
  vermillion: "#D55E00",
  purple: "#CC79A7",
};

const vehicle = {
  idle: "#8a8f98",
  moving: okabeIto.blue,
  stuck: okabeIto.vermillion,
  loading: okabeIto.sky,
  unloading: okabeIto.green,
  repairing: okabeIto.orange,
  broken: okabeIto.purple,
};

export const palettes: Record<Theme, Palette> = {
  light: {
    background: "#f7f7f5",
    road: "#c9ccd1",
    roadLabel: "#9aa0a8",
    text: "#1d2127",
    muted: "#5f6670",
    zoneCritical: "rgba(213, 94, 0, 0.13)",
    zoneEssential: "rgba(230, 159, 0, 0.12)",
    zoneResidential: "rgba(86, 180, 233, 0.12)",
    unpowered: "rgba(20, 24, 32, 0.45)",
    hatch: "rgba(20, 24, 32, 0.55)",
    blocked: okabeIto.vermillion,
    believedBlocked: okabeIto.purple,
    highlight: "#1d2127",
    substation: {
      online: okabeIto.green,
      failed: okabeIto.vermillion,
      tripped: okabeIto.orange,
      repairing: okabeIto.yellow,
    },
    vehicle,
    crew: okabeIto.orange,
    hospital: "#ffffff",
    hospitalDark: "#1d2127",
    barOk: okabeIto.green,
    barLow: okabeIto.vermillion,
    barTrack: "rgba(0, 0, 0, 0.12)",
    depot: okabeIto.blue,
  },
  dark: {
    background: "#15181d",
    road: "#3a404a",
    roadLabel: "#5d6571",
    text: "#e6e8eb",
    muted: "#9aa3ae",
    zoneCritical: "rgba(213, 94, 0, 0.20)",
    zoneEssential: "rgba(230, 159, 0, 0.16)",
    zoneResidential: "rgba(86, 180, 233, 0.16)",
    unpowered: "rgba(0, 0, 0, 0.6)",
    hatch: "rgba(230, 232, 235, 0.45)",
    blocked: "#ff7a3d",
    believedBlocked: "#e59ac4",
    highlight: "#ffffff",
    substation: {
      online: "#2bbf8f",
      failed: "#ff7a3d",
      tripped: okabeIto.orange,
      repairing: okabeIto.yellow,
    },
    vehicle: { ...vehicle, idle: "#a2a8b1", moving: "#3d9ae0" },
    crew: okabeIto.orange,
    hospital: "#e6e8eb",
    hospitalDark: "#000000",
    barOk: "#2bbf8f",
    barLow: "#ff7a3d",
    barTrack: "rgba(255, 255, 255, 0.15)",
    depot: "#3d9ae0",
  },
};
