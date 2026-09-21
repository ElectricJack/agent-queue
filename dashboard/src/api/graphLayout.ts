// Typed wrappers for the tiled task-graph layout endpoints.
//
// The daemon answers 202 (with an empty body) while a layout job is still
// running. The client interceptor in ./client throws only on non-2xx, so a 202
// lands here as an ordinary success with `response.status === 202` — callers
// get `{ pending: true }` and are expected to poll.
import { useEffect, useRef } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getExtentApiProjectsProjectIdGraphExtentGet,
  getJobApiProjectsProjectIdGraphJobsJobIdGet,
  postLocateApiProjectsProjectIdGraphLocatePost,
  getNodeApiProjectsProjectIdGraphNodeTaskIdGet,
  postListApiProjectsProjectIdGraphListPost,
  postTidyApiProjectsProjectIdGraphTidyPost,
  postTilesApiProjectsProjectIdGraphTilesPost,
  type ExtentResponse,
  type LayoutJob,
  type ListResponse,
  type LocateResponse,
  type NodeResponse,
  type TidyResponse,
  type TilesResponse,
} from "@aq/ts-client";
import { client } from "./client";
import type { Rect } from "../pages/command-center/layout-v2/units";
import { refetchLayout } from "../pages/command-center/layout-v2/liveRegistry";

export type Variant = "all" | "active";

/**
 * The canvas never expands a container in place (operator decision
 * 2026-09-20): `expanded` is always empty and the scope is chosen with
 * `root` instead. The wire request still carries the field, and the server
 * still honors a non-empty one for other callers.
 */
export interface TilesParams {
  variant: Variant;
  expanded: string[];
  root?: string | null;
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
      q: params.q ?? "",
      status: params.status ?? "",
    },
    throwOnError: true,
  });
  if (r.response.status === 202) return { pending: true };
  return r.data as TilesResponse;
}

export interface ListParams {
  variant: Variant;
  expanded: string[];
  q: string;
  status: string;
  cursor: string | null;
  limit: number;
}

/** One page of the flat, ordered node list backing the mobile view. */
export async function fetchList(
  projectId: string,
  body: ListParams,
  signal?: AbortSignal,
): Promise<ListResponse | { pending: true }> {
  const r = await postListApiProjectsProjectIdGraphListPost({
    client,
    signal,
    path: { project_id: projectId },
    body,
    throwOnError: true,
  });
  if (r.response.status === 202) return { pending: true };
  return r.data as ListResponse;
}

export const layoutExtentKey = (pid: string, variant: Variant) =>
  ["layoutExtent", pid, variant] as const;

/** Both variants of one project's extent — live updates invalidate the pair. */
export const layoutExtentPrefix = (pid: string) => ["layoutExtent", pid] as const;

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
      // Same cadence as the single-project hook: a building layout must not
      // leave the canvas waiting a minute for its extent.
      refetchInterval: (q: { state: { data?: ExtentResponse | { pending: true } } }) =>
        (q.state.data && "pending" in q.state.data ? 2000 : 60_000),
      staleTime: 30_000,
    })),
    combine: (results) => results.map((r) => r.data),
  });
}

/**
 * The `all` variant's total node count, read ONLY from whatever the query
 * cache already holds for it -- `enabled: false` means this never issues a
 * request of its own. It captions the empty-graph state with how many
 * finished tasks are hidden when that number is already known for free (the
 * operator toggled Show completed on and off again, or another view already
 * fetched it); otherwise it returns `null` and the caption leaves the number
 * out rather than paying for a fetch just to fill in a caption.
 */
export function useHiddenFinishedCount(
  projectIds: string[],
  variant: Variant,
): number | null {
  const results = useQueries({
    queries: projectIds.map((pid) => ({
      queryKey: layoutExtentKey(pid, "all"),
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchExtent(pid, "all", signal),
      enabled: false,
      staleTime: 30_000,
    })),
    combine: (results) => results.map((r) => r.data as ExtentResponse | { pending: true } | undefined),
  });
  // Only meaningful while active work is the one being hidden -- once the
  // canvas is already requesting `all`, there is nothing left to reveal.
  if (variant !== "active" || projectIds.length === 0) return null;
  let total = 0;
  for (const extent of results) {
    if (!extent || "pending" in extent) return null;
    total += extent.node_count;
  }
  return total;
}

export function useLayoutNode(projectId: string | undefined, taskId: string | null) {
  return useQuery({
    queryKey: ["layoutNode", projectId, taskId],
    enabled: !!projectId && !!taskId,
    queryFn: async ({ signal }): Promise<NodeResponse | { pending: true }> => {
      const r = await getNodeApiProjectsProjectIdGraphNodeTaskIdGet({
        client,
        signal,
        path: { project_id: projectId!, task_id: taskId! },
        query: { variant: "all" },
        throwOnError: true,
      });
      // A project with no published layout answers 202 and enqueues a
      // backfill; the body carries no node, so callers must not read one.
      if (r.response.status === 202) return { pending: true };
      return r.data as NodeResponse;
    },
    refetchInterval: (q) => (q.state.data && "pending" in q.state.data ? 2000 : false),
  });
}

export async function locate(
  projectId: string,
  variant: Variant,
  q: string,
  status: string,
  expanded: string[] = [],
): Promise<LocateResponse> {
  // `expanded` rides along because a hit's position depends on it: collapsing
  // a container reflows everything after it, so the persisted coordinate is
  // not where the canvas draws the match.
  const r = await postLocateApiProjectsProjectIdGraphLocatePost({
    client,
    path: { project_id: projectId },
    body: { variant, q, status, expanded },
    throwOnError: true,
  });
  return r.data as LocateResponse;
}

const JOB_POLL_MS = 2000;
const JOB_TIMEOUT_MS = 5 * 60_000;
const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Resolves once every job has settled, the bound expires, or `signal` aborts,
 * reporting the ids that settled as failed.
 */
async function awaitJobs(projectId: string, jobs: LayoutJob[], signal?: AbortSignal): Promise<string[]> {
  const failed: string[] = [];
  const waiting = new Set(jobs.map((job) => job.id));
  const deadline = Date.now() + JOB_TIMEOUT_MS;
  while (waiting.size > 0 && Date.now() < deadline && !signal?.aborted) {
    await delay(JOB_POLL_MS);
    if (signal?.aborted) return failed;
    for (const id of [...waiting]) {
      const r = await getJobApiProjectsProjectIdGraphJobsJobIdGet({
        client,
        signal,
        path: { project_id: projectId, job_id: id },
        throwOnError: true,
      });
      const status = (r.data as LayoutJob).status;
      // A failed job settles too: the caller reloads either way rather than
      // leaving a half-tidied graph on screen.
      if (status === "failed") failed.push(id);
      if (status === "done" || status === "failed") waiting.delete(id);
    }
  }
  return failed;
}

/**
 * Tidying re-runs the layout in the background, so the mutation stays pending
 * until the enqueued jobs settle — only then is a reload worth anything.
 */
export function useTidyLayout(projectId: string, clearManualPositions?: () => void) {
  const qc = useQueryClient();
  // Polling outlives the toolbar otherwise: an unmount must stop the loop
  // rather than keep hitting the daemon for a page nobody is looking at.
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);
  return useMutation({
    mutationFn: async () => {
      abort.current?.abort();
      const ac = new AbortController();
      abort.current = ac;
      const r = await postTidyApiProjectsProjectIdGraphTidyPost({
        client,
        signal: ac.signal,
        path: { project_id: projectId },
        body: {},
        throwOnError: true,
      });
      const failed = await awaitJobs(projectId, (r.data as TidyResponse).jobs ?? [], ac.signal);
      // A half-tidied graph still has to be reloaded (onSettled does that), but
      // the reader must be told the tidy did not finish.
      if (failed.length > 0) throw new Error(`layout job failed: ${failed.join(", ")}`);
      return r.data;
    },
    onSuccess: () => clearManualPositions?.(),
    onSettled: () => {
      // The extent moves and every tile is stale: the cached query and the
      // mounted layers both have to be told.
      void qc.invalidateQueries({ queryKey: ["layoutExtent", projectId] });
      refetchLayout(projectId);
    },
  });
}
