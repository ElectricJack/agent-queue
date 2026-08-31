import { useQuery } from "@tanstack/react-query";
import { client } from "./client";

// Kept local until the history endpoint is included in the generated SDK.
export interface TaskSessionAttempt {
  id: string;
  session_id: string;
  task_id: string;
  agent_id: string | null;
  agent_name: string | null;
  model: string | null;
  intelligence_class: string | null;
  harness: string | null;
  provider: string | null;
  state: string;
  work_dir: string | null;
  started_at: number;
  session_started_at: number | null;
  ended_at: number | null;
  end_reason: string | null;
  outcome: string | null;
  session_key: string | null;
}
interface TaskSessionsResponse {
  task_id: string;
  sessions: TaskSessionAttempt[];
}

export function useTaskSessions(taskId: string) {
  return useQuery({
    queryKey: ["task", taskId, "sessions"],
    queryFn: async () => {
      const { data } = await client.get<TaskSessionsResponse, unknown, true>({
        url: `/api/tasks/${encodeURIComponent(taskId)}/sessions`,
        throwOnError: true,
      });
      return data;
    },
    enabled: !!taskId,
    refetchInterval: 15_000,
  });
}
