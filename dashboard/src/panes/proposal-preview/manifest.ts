import { z } from "zod";
import { DocumentMagnifyingGlassIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const argsSchema = z.object({
  proposalId: z.string().min(1),
});

export type ProposalPreviewArgs = z.infer<typeof argsSchema>;

export const manifest: PaneManifest<ProposalPreviewArgs> = {
  id: "proposal-preview",
  name: "Proposal Preview",
  description: "Preview a staged task-batch proposal before approving it.",
  icon: DocumentMagnifyingGlassIcon,
  args_schema: argsSchema,
  // No open_shortcut — omitted entirely, never a literal null.
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Preview proposal",
  palette_section: "Proposals",
};
