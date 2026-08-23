import { z } from "zod";
import { ClipboardDocumentListIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const taskDetailArgsSchema = z.object({
  taskId: z.string().min(1),
});

export type TaskDetailArgs = z.infer<typeof taskDetailArgsSchema>;

export const manifest: PaneManifest<TaskDetailArgs> = {
  id: "task-detail",
  name: "Task",
  description: "Task status, actions, metadata, and relationships.",
  icon: ClipboardDocumentListIcon,
  args_schema: taskDetailArgsSchema,
  // No open_shortcut — reached via click-through, the palette action, or
  // agent push. Keeps keyboard slots free for less common views.
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Open task",
  palette_section: "Task",
};
