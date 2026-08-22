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
    })),
  });

  const merged: MergedGraph = {
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
      merged.tasks.push(t);
      merged.taskProject[t.id] = pid;
    }
    merged.edges.push(...((res.data.edges ?? []) as GraphEdge[]));
    merged.gates.push(...((res.data.gates ?? []) as GraphGate[]));
    merged.agents.push(...((res.data.agents ?? []) as GraphAgent[]));
  });

  return {
    data: merged,
    isLoading: results.some((r) => r.isLoading),
    errors: results.map((r) => (r.error as Error | null) ?? null),
  };
}
