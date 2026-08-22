import type { ComponentType } from "react";
import type { PaneManifest, PaneViewProps } from "./types";

export interface PaneEntry {
  manifest: PaneManifest;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  Component: ComponentType<PaneViewProps<any>>;
}

type ManifestModule = { manifest: PaneManifest };
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ComponentModule = { default: ComponentType<PaneViewProps<any>> };

const manifests = import.meta.glob<ManifestModule>("./*/manifest.ts", {
  eager: true,
});
const components = import.meta.glob<ComponentModule>("./*/index.tsx", {
  eager: true,
});

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
    const componentMod = components[componentPath];
    if (!componentMod) {
      throw new Error(
        `pane registry: view "${id}" is missing index.tsx default export`,
      );
    }
    if (out[id]) {
      throw new Error(`pane registry: duplicate view id "${id}"`);
    }
    out[id] = { manifest: mod.manifest, Component: componentMod.default };
  }
  return out;
}

export const PANE_REGISTRY: Record<string, PaneEntry> = buildRegistry();
