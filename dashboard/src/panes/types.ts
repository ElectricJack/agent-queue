import type { ComponentType, SVGProps } from "react";
import type { z } from "zod";

export type HeroIcon = ComponentType<SVGProps<SVGSVGElement>>;

export interface PaneManifest<TArgs = unknown> {
  id: string;
  name: string;
  description: string;
  icon: HeroIcon;
  args_schema?: z.ZodType<TArgs>;
  /** Omit or `undefined` = no shortcut. NEVER literal `null`. */
  open_shortcut?: string;
  route_scope?: "cross-route" | "route-scoped";
  agent_pushable?: boolean;
  palette_label?: string | null;
  palette_section?: string;
}

export interface PaneToolbarAction {
  id: string;
  label: string;
  icon?: HeroIcon;
  onClick: () => void;
  disabled?: boolean;
}

export interface ShortcutBinding {
  key: string;
  label: string;
  onFire: () => void;
}

export interface PaneViewProps<TArgs = unknown> {
  args: TArgs;
  close: () => void;
  setArgs: (next: TArgs) => void;
  setToolbar: (actions: PaneToolbarAction[]) => void;
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}
