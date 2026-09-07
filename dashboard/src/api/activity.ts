import { useQuery } from "@tanstack/react-query";
import { client } from "./client";

// Kept local rather than imported from the generated SDK for the same reason
// as taskSessions.ts: one small read the dashboard owns end to end.
export interface TaskActivityAttempt {
  id: string;
  session_id: string;
  task_id: string;
  agent_id: string | null;
  agent_name: string | null;
  profile_id: string | null;
  model: string | null;
  intelligence_class: string | null;
  llm_provider: string | null;
  harness: string | null;
  provider: string | null;
  state: string;
  started_at: number;
  ended_at: number | null;
  end_reason: string | null;
  outcome: string | null;
}

export interface TaskActivityItem {
  task_id: string;
  project_id: string | null;
  title: string;
  status: string;
  priority: number | null;
  parent_task_id: string | null;
  archived: boolean;
  created_at: number | null;
  updated_at: number | null;
  last_activity_at: number;
  attempts: TaskActivityAttempt[];
  attempt_count: number;
  /** Distinct models the attempts reported, newest attempt first. */
  models: string[];
  unattributed_attempts: number;
  outcome: string | null;
  work_outcome: string | null;
  failure_class: string | null;
  completed_at: number | null;
  summary: string;
  pr_url: string | null;
}

export interface TaskActivityModelTotal {
  model: string | null;
  tasks: number;
  attempts: number;
}

export interface TaskRecentActivityResponse {
  since: number;
  until: number;
  hours: number;
  project_id: string | null;
  items: TaskActivityItem[];
  total: number;
  truncated: boolean;
  by_model: TaskActivityModelTotal[];
}

/** Tasks worked on in the last `hours`; disabled (and not fetched) when null. */
export function useRecentActivity(hours: number | null, projectId?: string) {
  return useQuery({
    queryKey: ["task-recent-activity", hours, projectId ?? ""],
    queryFn: async () => {
      const { data } = await client.post<TaskRecentActivityResponse, unknown, true>({
        url: "/api/task/recent-activity",
        body: { hours, project_id: projectId ?? null, limit: 500 },
        throwOnError: true,
      });
      return data;
    },
    enabled: hours !== null,
    refetchInterval: 30_000,
  });
}
