import { z } from "zod";
import { ArrowPathIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";
export const argsSchema = z.object({ playbookId: z.string().min(1) });
export type PlaybookDetailArgs = z.infer<typeof argsSchema>;
export const manifest: PaneManifest<PlaybookDetailArgs> = {
  id: "playbook-detail", name: "Playbook", description: "Persistent playbook definition, triggers, and run history.",
  icon: ArrowPathIcon, args_schema: argsSchema, route_scope: "cross-route",
  agent_pushable: true, palette_label: "Inspect playbook", palette_section: "Playbooks",
};
