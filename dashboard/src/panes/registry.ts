import { lazy, type ComponentType } from "react";
import type { PaneManifest, PaneViewProps } from "./types";

export interface PaneEntry {
  manifest: PaneManifest;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  Component: ComponentType<PaneViewProps<any>>;
  /** Fetches the view's code ahead of the first open; resolves once loaded. */
  preload?: () => Promise<unknown>;
}

type ManifestModule = { manifest: PaneManifest };
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ComponentModule = { default: ComponentType<PaneViewProps<any>> };

const manifests = import.meta.glob<ManifestModule>("./*/manifest.ts", {
  eager: true,
});
// Components are code-split: each view (and whatever it pulls in — markdown,
// React Flow, dagre, the spec reader's unified pipeline) is its own chunk,
// fetched the first time the view opens. Eager, they were all part of the
// entry chunk that every page load parses before the first route renders.
const components = import.meta.glob<ComponentModule>("./*/index.tsx");

function dirOf(path: string): string {
  // "./task-detail/manifest.ts" -> "task-detail"
  return path.split("/")[1] ?? "";
}

function buildRegistry(): Record<string, PaneEntry> {
  const out: Record<string, PaneEntry> = {};
  for (const [manifestPath, mod] of Object.entries(manifests)) {
    const id = dirOf(manifestPath);
    if (!id) continue;
    if (mod.manifest.id !== id) {
      throw new Error(
        `pane registry: manifest id "${mod.manifest.id}" does not match directory "${id}"`,
      );
    }
    const componentPath = `./${id}/index.tsx`;
    const load = components[componentPath];
    if (!load) {
      throw new Error(
        `pane registry: view "${id}" is missing index.tsx default export`,
      );
    }
    if (out[id]) {
      throw new Error(`pane registry: duplicate view id "${id}"`);
    }
    out[id] = { manifest: mod.manifest, Component: lazy(load), preload: load };
  }
  return out;
}

export const PANE_REGISTRY: Record<string, PaneEntry> = buildRegistry();
