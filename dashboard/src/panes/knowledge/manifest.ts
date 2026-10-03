import { z } from "zod";
import { BookOpenIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

const schema = z.object({ projectId: z.string().min(1), recordId: z.string().min(1), revisionId: z.string().nullable().optional() });
export type KnowledgeArgs = z.infer<typeof schema>;
export const manifest: PaneManifest<KnowledgeArgs> = {
  id: "knowledge", name: "Knowledge", description: "Read an authorized knowledge revision.",
  icon: BookOpenIcon, args_schema: schema, route_scope: "cross-route", agent_pushable: true,
};
