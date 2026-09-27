import { queryOptions, useQuery } from "@tanstack/react-query";
import { collaborationGet, collaborationList } from "./client";

/** Threads change on agent sends and the expiry tick; neither emits a task event. */
export const COLLABORATION_REFETCH_MS = 15_000;

export const taskCollaborationsQuery = (taskId: string) => queryOptions({
  // Under the task key so task.updated invalidation refreshes it too.
  queryKey: ["task", taskId, "collaborations"],
  queryFn: async () => (await collaborationList({
    body: { task_id: taskId }, throwOnError: true,
  })).data,
});

export const collaborationThreadQuery = (threadId: string) => queryOptions({
  queryKey: ["collaboration", threadId],
  // No after_seq: the daemon returns the latest messages, in ascending seq.
  queryFn: async () => (await collaborationGet({
    body: { thread_id: threadId }, throwOnError: true,
  })).data,
});

export function useTaskCollaborations(taskId: string) {
  return useQuery({
    ...taskCollaborationsQuery(taskId),
    enabled: !!taskId,
    refetchInterval: COLLABORATION_REFETCH_MS,
  });
}

export function useCollaborationThread(threadId: string) {
  return useQuery({
    ...collaborationThreadQuery(threadId),
    enabled: !!threadId,
    refetchInterval: COLLABORATION_REFETCH_MS,
  });
}
