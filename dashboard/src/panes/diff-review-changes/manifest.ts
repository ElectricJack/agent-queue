import { z } from "zod";
import { DocumentTextIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const diffReviewChangesArgsSchema = z.object({
  taskId: z.string().min(1),
  base: z.string().min(1).optional(),
  filePath: z.string().min(1).optional(),
});

export type DiffReviewChangesArgs = z.infer<typeof diffReviewChangesArgsSchema>;

export const manifest: PaneManifest<DiffReviewChangesArgs> = {
  id: "diff-review-changes",
  name: "Review changes",
  description: "Task worktree diff — changed files vs base, with preview.",
  icon: DocumentTextIcon,
  args_schema: diffReviewChangesArgsSchema,
  open_shortcut: "$mod-shift-d",
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Review changes",
  palette_section: "Task",
};
