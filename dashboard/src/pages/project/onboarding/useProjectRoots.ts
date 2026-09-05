import { QueryClient, QueryClientContext, useQuery } from "@tanstack/react-query";
import { useContext } from "react";
import { listProjectRoots } from "../../../api/client";

export interface ProjectRootSummary {
  id: string;
  label: string;
  /** Operator-facing path (may be abbreviated, e.g. `~/dev`). */
  displayPath: string;
  readable: boolean;
  writable: boolean;
}

export type ProjectRootsSource =
  | { status: "loading" }
  | { status: "ready"; roots: ProjectRootSummary[] }
  | { status: "error"; message: string };

// Some isolated navigation/story tests render the rail without the app's
// QueryClientProvider. Keep that read-only preview safe without changing the
// production path, which always uses the provider's shared cache.
const previewQueryClient = new QueryClient();

/** Fetch roots through the generated client; the browser holds no filesystem capability. */
export function useProjectRoots(): ProjectRootsSource {
  const providedQueryClient = useContext(QueryClientContext);
  const query = useQuery({
    queryKey: ["project-roots"],
    queryFn: async () => {
      const { data } = await listProjectRoots({ body: {}, throwOnError: true });
      return (data.roots ?? []).map((root) => ({
        id: root.id,
        label: root.label,
        displayPath: root.path,
        readable: root.readable ?? false,
        writable: root.writable ?? false,
      }));
    },
    enabled: providedQueryClient !== undefined,
  }, providedQueryClient ?? previewQueryClient);
  if (query.isPending) return { status: "loading" };
  if (query.isError) {
    return { status: "error", message: query.error instanceof Error ? query.error.message : "Could not load project roots." };
  }
  return { status: "ready", roots: query.data ?? [] };
}
