import { z } from "zod";
import { PlayIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const argsSchema = z.object({
  runId: z.string().min(1),
  taskId: z.string().min(1).optional(),
});
export type PlaybookRunInspectorArgs = z.infer<typeof argsSchema>;

export const manifest: PaneManifest<PlaybookRunInspectorArgs> = {
  id: "playbook-run-inspector",
  name: "Playbook Run",
  description: "Live node states, outputs, and HITL gates for one playbook run.",
  icon: PlayIcon,
  args_schema: argsSchema,
  // No open_shortcut — reached only by click-through or agent push, never a
  // global hotkey (field intentionally omitted, not set to null).
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Inspect playbook run",
  palette_section: "Playbooks",
};
