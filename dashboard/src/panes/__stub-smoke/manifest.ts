import { z } from "zod";
import { BeakerIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const manifest: PaneManifest<{ text: string }> = {
  id: "__stub-smoke",
  name: "Stub (smoke test only)",
  description: "Test-only view for pane infrastructure smoke.",
  icon: BeakerIcon,
  args_schema: z.object({ text: z.string() }),
  agent_pushable: true,
  palette_label: null,
};
