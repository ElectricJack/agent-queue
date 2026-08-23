import { z } from "zod";
import { FolderOpenIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const fileBrowserArgsSchema = z.object({
  workspaceId: z.string().min(1),
  path: z.string().default(""),
});

export type FileBrowserArgs = z.infer<typeof fileBrowserArgsSchema>;

export const manifest: PaneManifest<FileBrowserArgs> = {
  id: "file-browser",
  name: "File Browser",
  description: "Browse files in a workspace and preview their contents.",
  icon: FolderOpenIcon,
  args_schema: fileBrowserArgsSchema,
  open_shortcut: "$mod-shift-f",
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Browse files",
  palette_section: "Workspace",
};
