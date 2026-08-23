import { z } from "zod";
import { BookOpenIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const argsSchema = z
  .object({
    workspaceId: z.string().optional(),
    path: z.string().optional(),
    url: z.string().optional(),
  })
  .superRefine((val, ctx) => {
    const hasWorkspacePath = val.workspaceId !== undefined && val.path !== undefined;
    const hasUrl = val.url !== undefined;

    if (hasWorkspacePath === hasUrl) {
      // both present, or neither present — reject either way
      ctx.addIssue({
        code: "custom",
        message: "requires exactly one of (workspaceId + path) or url",
      });
      return;
    }
    if (val.workspaceId !== undefined && val.path === undefined) {
      ctx.addIssue({
        code: "custom",
        path: ["path"],
        message: "path is required when workspaceId is set",
      });
    }
    if (val.path !== undefined && val.workspaceId === undefined) {
      ctx.addIssue({
        code: "custom",
        path: ["workspaceId"],
        message: "workspaceId is required when path is set",
      });
    }
  });

export type SpecDocReaderArgs = z.infer<typeof argsSchema>;

export const manifest: PaneManifest<SpecDocReaderArgs> = {
  id: "spec-doc-reader",
  name: "Spec Reader",
  description: "Read a spec or design doc with table of contents and frontmatter summary.",
  icon: BookOpenIcon,
  args_schema: argsSchema,
  // No open_shortcut — omitted entirely, never literal null.
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Read spec",
  palette_section: "Docs",
};
