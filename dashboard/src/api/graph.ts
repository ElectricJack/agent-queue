import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";
import {
  getProjectGraphApiProjectsProjectIdGraphGet,
  type ProjectGraphResponse,
} from "@aq/ts-client";
import { client } from "./client";
import type {
  MergedGraph,
  GraphTaskNode,
  GraphEdge,
  GraphGate,
  GraphAgent,
} from "../pages/command-center/types";

export const projectGraphKey = (pid: string) => ["projectGraph", pid] as const;

/** Shared by the initial-route prefetch and the mounted workspace. */
export function projectGraphQuery(pid: string) {
  return {
    queryKey: projectGraphKey(pid),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<ProjectGraphResponse> => {
      const r = await getProjectGraphApiProjectsProjectIdGraphGet({
        client,
        signal,
        path: { project_id: pid },
        throwOnError: true,
      });
      return r.data as ProjectGraphResponse;
    },
    // Background reconciliation — belt to the WS suspenders.
    refetchInterval: 60_000,
    staleTime: 30_000,
    // A missing project must fail fast instead of holding the canvas behind
    // React Query's default 1s + 2s + 4s retry backoff.
    retry: 1,
    retryDelay: 250,
  };
}

export function useProjectGraphs(projectIds: string[]) {
  const results = useQueries({
    queries: projectIds.map(projectGraphQuery),
  });

  // `merged` must keep its identity between renders: GraphCanvas memoises the
  // dagre layout on this object, so a fresh literal every render means the
  // layout is recomputed every render (~78ms at 500 nodes).
  const merged = useMemo<MergedGraph>(() => {
    const acc: MergedGraph = {
      tasks: [],
      edges: [],
      gates: [],
      agents: [],
      taskProject: {},
    };
    results.forEach((res, i) => {
      if (!res.data) return;
      const pid = projectIds[i];
      if (!pid) return;
      for (const t of (res.data.tasks ?? []) as GraphTaskNode[]) {
        acc.tasks.push(t);
        acc.taskProject[t.id] = pid;
      }
      acc.edges.push(...((res.data.edges ?? []) as GraphEdge[]));
      acc.gates.push(...((res.data.gates ?? []) as GraphGate[]));
      acc.agents.push(...((res.data.agents ?? []) as GraphAgent[]));
    });
    return acc;
    // Re-merge only when a project's payload actually changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectIds.join(","), ...results.map((r) => r.data)]);

  return {
    data: merged,
    // An errored project contributes nothing and is not "still loading" —
    // counting it here is what stalled the canvas behind a dead project id.
    isLoading: results.some((r) => r.isLoading && !r.isError),
    errors: results.map((r) => (r.error as Error | null) ?? null),
  };
}
