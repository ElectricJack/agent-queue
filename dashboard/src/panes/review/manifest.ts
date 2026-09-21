import { DocumentMagnifyingGlassIcon } from "@heroicons/react/24/outline";
import { z } from "zod";

import type { PaneManifest } from "../types";

export const reviewArgsSchema = z.object({ reviewId: z.string().min(1) });
export type ReviewArgs = z.infer<typeof reviewArgsSchema>;

export const manifest: PaneManifest<ReviewArgs> = {
  id: "review",
  name: "Review",
  description: "Read a submitted document, leave anchored comments, and record a decision.",
  icon: DocumentMagnifyingGlassIcon,
  args_schema: reviewArgsSchema,
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: null,
};
