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

export function useProjectGraphs(projectIds: string[]) {
  const results = useQueries({
    queries: projectIds.map((pid) => ({
      queryKey: projectGraphKey(pid),
      queryFn: async (): Promise<ProjectGraphResponse> => {
        const r = await getProjectGraphApiProjectsProjectIdGraphGet({
          client,
          path: { project_id: pid },
          throwOnError: true,
        });
        return r.data as ProjectGraphResponse;
      },
      // Background reconciliation — belt to the WS suspenders.
      refetchInterval: 60_000,
      staleTime: 30_000,
      // A project that cannot be fetched (deleted id left in the persisted
      // selection, daemon briefly down) must fail fast. On React Query's
      // default retry: 3 the backoff runs 1s + 2s + 4s, and because the
      // canvas waits on `isLoading` below, one bad id held the whole graph
      // at "Loading…" for ~7s on every visit.
      retry: 1,
      retryDelay: 250,
    })),
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
