import { z } from "zod";
import { CommandLineIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const sessionPeekArgsSchema = z.object({
  sessionId: z.string().min(1),
});

export type SessionPeekArgs = z.infer<typeof sessionPeekArgsSchema>;

export const manifest: PaneManifest<SessionPeekArgs> = {
  id: "session-peek",
  name: "Session Peek",
  description: "Live tmux peek stream for one session.",
  icon: CommandLineIcon,
  args_schema: sessionPeekArgsSchema,
  // open_shortcut omitted per interface spec — undefined means "no shortcut",
  // literal null is never used (dashboard/src/panes/types.ts field docs).
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Peek session",
  palette_section: "Sessions",
};
