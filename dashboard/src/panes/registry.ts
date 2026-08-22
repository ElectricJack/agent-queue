import type { ComponentType } from "react";
import type { PaneManifest, PaneViewProps } from "./types";

export interface PaneEntry {
  manifest: PaneManifest;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  Component: ComponentType<PaneViewProps<any>>;
}

// Populated by Task 3 via import.meta.glob. Empty until then; store is
// still functional in tests via registryOverride.
export const PANE_REGISTRY: Record<string, PaneEntry> = {};
