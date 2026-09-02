// Typed wrappers for the tiled task-graph layout endpoints.
//
// The daemon answers 202 (with an empty body) while a layout job is still
// running. The client interceptor in ./client throws only on non-2xx, so a 202
// lands here as an ordinary success with `response.status === 202` — callers
// get `{ pending: true }` and are expected to poll.
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getExtentApiProjectsProjectIdGraphExtentGet,
  getLocateApiProjectsProjectIdGraphLocateGet,
  getNodeApiProjectsProjectIdGraphNodeTaskIdGet,
  postTidyApiProjectsProjectIdGraphTidyPost,
  postTilesApiProjectsProjectIdGraphTilesPost,
  type ExtentResponse,
  type LocateResponse,
  type NodeResponse,
  type TilesResponse,
} from "@aq/ts-client";
import { client } from "./client";
import type { Rect } from "../pages/command-center/layout-v2/units";

export type Variant = "all" | "active";

export interface TilesParams {
  variant: Variant;
  expanded: string[];
  root?: string | null;
  maxDepth?: number | null;
  q?: string;
  status?: string;
}

export async function fetchTiles(
  projectId: string,
  rect: Rect,
  params: TilesParams,
  signal?: AbortSignal,
): Promise<TilesResponse | { pending: true }> {
  const r = await postTilesApiProjectsProjectIdGraphTilesPost({
    client,
    signal,
    path: { project_id: projectId },
    body: {
      variant: params.variant,
      rect,
      expanded: params.expanded,
      root: params.root ?? null,
      max_depth: params.maxDepth ?? null,
      q: params.q ?? "",
      status: params.status ?? "",
    },
    throwOnError: true,
  });
  if (r.response.status === 202) return { pending: true };
  return r.data as TilesResponse;
}

export const layoutExtentKey = (pid: string, variant: Variant) =>
  ["layoutExtent", pid, variant] as const;

async function fetchExtent(
  projectId: string,
  variant: Variant,
  signal?: AbortSignal,
): Promise<ExtentResponse | { pending: true }> {
  const r = await getExtentApiProjectsProjectIdGraphExtentGet({
    client,
    signal,
    path: { project_id: projectId },
    query: { variant },
    throwOnError: true,
  });
  if (r.response.status === 202) return { pending: true };
  return r.data as ExtentResponse;
}

export function useLayoutExtent(projectId: string | undefined, variant: Variant) {
  return useQuery({
    queryKey: layoutExtentKey(projectId ?? "", variant),
    enabled: !!projectId,
    queryFn: ({ signal }) => fetchExtent(projectId!, variant, signal),
    // Poll fast while the layout job is still building, slowly once it is not.
    refetchInterval: (q) => (q.state.data && "pending" in q.state.data ? 2000 : 60_000),
    staleTime: 30_000,
  });
}

export function useLayoutExtents(
  projectIds: string[],
  variant: Variant,
): (ExtentResponse | { pending: true } | undefined)[] {
  return useQueries({
    queries: projectIds.map((pid) => ({
      queryKey: layoutExtentKey(pid, variant),
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchExtent(pid, variant, signal),
      refetchInterval: 60_000,
      staleTime: 30_000,
    })),
    combine: (results) => results.map((r) => r.data),
  });
}

export function useLayoutNode(projectId: string | undefined, taskId: string | null) {
  return useQuery({
    queryKey: ["layoutNode", projectId, taskId],
    enabled: !!projectId && !!taskId,
    queryFn: async ({ signal }): Promise<NodeResponse> => {
      const r = await getNodeApiProjectsProjectIdGraphNodeTaskIdGet({
        client,
        signal,
        path: { project_id: projectId!, task_id: taskId! },
        query: { variant: "all" },
        throwOnError: true,
      });
      return r.data as NodeResponse;
    },
  });
}

export async function locate(
  projectId: string,
  variant: Variant,
  q: string,
  status: string,
): Promise<LocateResponse> {
  const r = await getLocateApiProjectsProjectIdGraphLocateGet({
    client,
    path: { project_id: projectId },
    query: { variant, q, status },
    throwOnError: true,
  });
  return r.data as LocateResponse;
}

export function useTidyLayout(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const r = await postTidyApiProjectsProjectIdGraphTidyPost({
        client,
        path: { project_id: projectId },
        body: {},
        throwOnError: true,
      });
      return r.data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["layoutExtent", projectId] }),
  });
}
