// Loading the exported files. They sit next to the page in data/ (see vite.config.ts and
// `blackout export-viewer --out viewer/public/data`). Each file is fetched once and cached.

import { VIEWER_DATA_VERSION, type Manifest, type Run, type Scenario } from "./types";

const DATA = `${import.meta.env.BASE_URL}data/`;
const cache = new Map<string, Promise<unknown>>();

function load<T>(path: string): Promise<T> {
  let pending = cache.get(path);
  if (!pending) {
    pending = fetch(DATA + path).then(async (response) => {
      if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
      const data = (await response.json()) as { v?: number };
      if (data.v !== VIEWER_DATA_VERSION) {
        throw new Error(
          `${path} is in viewer data version ${data.v}, this viewer reads ${VIEWER_DATA_VERSION}: re-run blackout export-viewer`,
        );
      }
      return data;
    });
    pending.catch(() => cache.delete(path)); // let a failed fetch be retried
    cache.set(path, pending);
  }
  return pending as Promise<T>;
}

export const loadManifest = () => load<Manifest>("manifest.json");
export const loadScenario = (file: string) => load<Scenario>(file);
export const loadRun = (file: string) => load<Run>(file);
