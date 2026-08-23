import { z } from "zod";
import { CommandLineIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const consoleStreamArgsSchema = z.object({
  streamId: z.string().min(1),
  title: z.string().optional(),
  sessionId: z.string().optional(),
});
export type ConsoleStreamArgs = z.infer<typeof consoleStreamArgsSchema>;

export const manifest: PaneManifest<ConsoleStreamArgs> = {
  id: "console-stream",
  name: "Console",
  description: "Live stdout/stderr for a running command.",
  icon: CommandLineIcon,
  args_schema: consoleStreamArgsSchema,
  // No open_shortcut — agent-push is the primary opener (spec §3).
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: null,
};
