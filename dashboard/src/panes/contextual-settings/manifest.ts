import { Cog6ToothIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";
import { contextualSettingsArgsSchema, type ContextualSettingsArgs } from "./args";

export const manifest: PaneManifest<ContextualSettingsArgs> = {
  id: "contextual-settings",
  name: "Settings",
  description: "Edit a project, profile, playbook, or intelligence class inline.",
  icon: Cog6ToothIcon,
  args_schema: contextualSettingsArgsSchema,
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Open settings for…",
  palette_section: "Settings",
};
