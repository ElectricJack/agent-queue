import { useQuery } from "@tanstack/react-query";
import { waitExplain, waitGraph } from "./client";

export type { WaitExplanation, WaitGraphProject } from "./client";

export function useWaitExplain(taskId: string, projectId: string) {
  return useQuery({
    queryKey: ["wait-explain", taskId, projectId],
    queryFn: async ({ signal }) => (await waitExplain({
      body: { task_id: taskId, project_id: projectId }, signal, throwOnError: true,
    })).data!,
    enabled: !!taskId && !!projectId,
  });
}

export function useWaitGraph(projectId?: string) {
  return useQuery({
    queryKey: ["wait-graph", projectId ?? "fleet"],
    queryFn: async ({ signal }) => {
      const { data } = await waitGraph({
        body: projectId ? { project_id: projectId } : { fleet: true }, signal, throwOnError: true,
      });
      return data!.graph ?? data!;
    },
  });
}
