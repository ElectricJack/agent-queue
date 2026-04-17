import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost, apiGet } from "./client";

// --- System ---

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<{ status: string }>("/health"),
    refetchInterval: 60_000,
  });
}

export function useSystemStatus() {
  return useQuery({
    queryKey: ["system", "status"],
    queryFn: () => apiPost<Record<string, unknown>>("/system/get-status"),
    refetchInterval: 60_000,
  });
}

// --- Agents ---

export interface Agent {
  workspace_id: string;
  project_id: string;
  name: string;
  state: string;
  current_task_id?: string | null;
  current_task_title?: string | null;
}

export function useAgents(projectId?: string) {
  return useQuery({
    queryKey: ["agents", projectId],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (projectId) body.project_id = projectId;
      const data = await apiPost<{ success: boolean; agents: Agent[] }>("/agent/list", body);
      return data.agents ?? [];
    },
    refetchInterval: 60_000,
    enabled: !!projectId,
  });
}

export function useAllAgents(projectIds: string[]) {
  return useQuery({
    queryKey: ["agents", "all", projectIds],
    queryFn: async () => {
      const results = await Promise.all(
        projectIds.map((pid) =>
          apiPost<{ success: boolean; agents: Agent[] }>("/agent/list", { project_id: pid })
        ),
      );
      return results.flatMap((r) => r.agents ?? []);
    },
    refetchInterval: 60_000,
    enabled: projectIds.length > 0,
  });
}

// --- Tasks ---

export interface TaskRef {
  id: string;
  title: string;
  status?: string;
}

export interface Task {
  id: string;
  title: string;
  status: string;
  description?: string;
  priority?: number;
  project_id?: string;
  assigned_agent?: string | null;
  agent_name?: string | null;
  task_type?: string | null;
  profile_id?: string | null;
  pr_url?: string | null;
  retry_count?: number;
  max_retries?: number;
  requires_approval?: boolean;
  is_plan_subtask?: boolean;
  auto_approve_plan?: boolean;
  parent_task_id?: string | null;
  depends_on?: TaskRef[];
  blocks?: TaskRef[];
  subtasks?: TaskRef[];
  created_at?: string;
  updated_at?: string;
}

export function useTasks(projectId?: string, opts?: { showAll?: boolean }) {
  return useQuery({
    queryKey: ["tasks", projectId, opts?.showAll],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (projectId) body.project_id = projectId;
      if (opts?.showAll) body.show_all = true;
      const data = await apiPost<{ success: boolean; tasks: Task[] }>("/task/list", body);
      return data.tasks ?? [];
    },
    refetchInterval: 60_000,
  });
}

export function useTask(taskId: string) {
  return useQuery({
    queryKey: ["task", taskId],
    queryFn: () => apiPost<Task>("/task/get", { task_id: taskId }),
    refetchInterval: 60_000,
    enabled: !!taskId,
  });
}

export function useActiveTasksAllProjects() {
  return useQuery({
    queryKey: ["tasks", "active", "all"],
    queryFn: async () => {
      const data = await apiPost<{ success: boolean; tasks: Task[] }>("/task/list-active-all-projects");
      return data.tasks ?? [];
    },
    refetchInterval: 60_000,
  });
}

// --- Projects ---

export interface Project {
  id: string;
  name: string;
  repo_path?: string;
  default_branch?: string;
  is_active?: boolean;
  paused?: boolean;
}

export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const data = await apiPost<{ success: boolean; projects: Project[] }>("/project/list");
      return data.projects ?? [];
    },
    refetchInterval: 30_000,
  });
}

// --- Task Mutations ---

function useTaskMutation<TInput extends Record<string, unknown>, TOutput = unknown>(
  endpoint: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: TInput) => apiPost<TOutput>(endpoint, input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task"] });
    },
  });
}

export function useStopTask() {
  return useTaskMutation<{ task_id: string }, { stopped: string }>("/task/stop");
}

export function useRestartTask() {
  return useTaskMutation<{ task_id: string }, { restarted: string; title: string; previous_status: string }>("/task/restart");
}

export function useSkipTask() {
  return useTaskMutation<{ task_id: string }, { skipped: string; unblocked_count: number }>("/task/skip");
}

export function useApproveTask() {
  return useTaskMutation<{ task_id: string }, { approved: string; title: string }>("/task/approve");
}

export function useApprovePlan() {
  return useTaskMutation<{ task_id: string }, { approved: string; subtask_count: number }>("/task/approve-plan");
}

export function useRejectPlan() {
  return useTaskMutation<{ task_id: string; feedback: string }, { rejected: string }>("/task/reject-plan");
}

export function useDeletePlan() {
  return useTaskMutation<{ task_id: string }, { deleted: string; draft_subtasks_deleted: number }>("/task/delete-plan");
}

export function useReopenWithFeedback() {
  return useTaskMutation<{ task_id: string; feedback: string }, { reopened: string }>("/task/reopen-with-feedback");
}

export function useEditTask() {
  return useTaskMutation<Record<string, unknown>, { updated: string; fields: string[] }>("/task/edit");
}

export function useDeleteTask() {
  return useTaskMutation<{ task_id: string }, { deleted: string; title: string }>("/task/delete");
}

export function useCreateTask() {
  return useTaskMutation<Record<string, unknown>, { created: string; title: string }>("/task/create");
}

export function useProvideInput() {
  return useTaskMutation<{ task_id: string; input: string }>("/system/provide-input");
}
