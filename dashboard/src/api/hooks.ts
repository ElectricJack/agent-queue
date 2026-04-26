import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiGet } from "./legacy-fetch";
import {
  approvePlan,
  approveTask,
  createMcpServer,
  createPlaybook,
  createProjectProfile,
  createTask,
  deleteMcpServer,
  deletePlan,
  deletePlaybook,
  deleteProject,
  deleteProjectProfile,
  deleteTask,
  editMcpServer,
  editProjectProfile,
  editTask,
  getMcpServer,
  getProject,
  getStatus,
  getTask,
  listActiveTasksAllProjects,
  listAgents,
  listEventTriggers,
  listMcpServers,
  listMcpToolCatalog,
  listPlaybookRuns,
  listPlaybooks,
  listProfiles,
  listProjectProfiles,
  listProjects,
  listTasks,
  listWorkspaces,
  orchestratorControl,
  pauseProject,
  probeMcpServer,
  provideInput,
  rejectPlan,
  reopenWithFeedback,
  restartTask,
  resumeProject,
  showEffectiveProfile,
  skipTask,
  stopTask,
  updatePlaybookSource,
  getPlaybookSource,
} from "./client";
import type {
  AgentSummary,
  CatalogEntryModel,
  TaskRef,
  CreateMcpServerRequest,
  CreateProjectProfileRequest,
  CreateTaskRequest,
  CreateProjectProfileResponse2 as CreateProjectProfileResponse,
  EditProjectProfileRequest,
  EditTaskRequest,
  EventTrigger,
  GetMcpServerResponse,
  GetProjectResponse2 as ProjectResponse,
  GetStatusResponse2 as SystemStatusResponse,
  GetTaskResponse2 as TaskResponse,
  ListEventTriggersResponse,
  ListMcpServersResponse,
  ListMcpToolCatalogResponse,
  ListPlaybookRunsResponse,
  ListPlaybooksResponse,
  ListProfilesResponse2 as ListProfilesResponse,
  ListProjectProfilesResponse,
  ListProjectsResponse2 as ListProjectsResponse,
  ListTasksResponse2 as ListTasksResponse,
  ListWorkspacesResponse2 as ListWorkspacesResponse,
  McpServerSummary,
  OrchestratorControlResponse2 as OrchestratorControlResponse,
  PlaybookRunSummary,
  PlaybookSummary,
  ProbedToolModel,
  ProbeMcpServerResponse,
  ProfileDetail,
  ProfileSummary,
  ProjectProfileRow,
  ProjectSummary,
  ShowEffectiveProfileResponse,
  TaskDetail,
  WorkspaceSummary,
  GetPlaybookSourceResponse,
  UpdatePlaybookSourceResponse,
} from "./client";

// --- Re-exports — call sites should import shared types from here ---
export type {
  AgentSummary,
  AgentSummary as Agent,
  CatalogEntryModel as CatalogEntry,
  TaskRef,
  CreateMcpServerRequest,
  CreateProjectProfileRequest,
  CreateProjectProfileResponse,
  CreateTaskRequest,
  EditProjectProfileRequest,
  EditTaskRequest,
  EventTrigger,
  GetMcpServerResponse as McpServerDetail,
  ListPlaybookRunsResponse,
  ListPlaybooksResponse,
  McpServerSummary,
  PlaybookRunSummary,
  PlaybookSummary,
  ProbedToolModel as ProbedTool,
  ProfileDetail,
  ProfileSummary as Profile,
  ProjectProfileRow,
  ProjectResponse as Project,
  ProjectSummary,
  ShowEffectiveProfileResponse,
  TaskResponse as Task,
  TaskDetail,
  WorkspaceSummary as Workspace,
  GetPlaybookSourceResponse as PlaybookSource,
  UpdatePlaybookSourceResponse as PlaybookUpdateResult,
};

// Convenience: every project response gets a derived `paused` boolean so
// existing UI code can keep doing `project.paused` regardless of whether the
// daemon returned status="PAUSED" or paused=true.
type Pausable = { status?: string | null; paused?: boolean | null };
function withPaused<T extends Pausable>(p: T): T & { paused: boolean } {
  return { ...p, paused: Boolean(p.paused ?? p.status === "PAUSED") };
}

// --- Health (non-codegen routes — these stay on the legacy fetch) ---

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<{ status: string }>("/health"),
    refetchInterval: 60_000,
  });
}

// --- System ---

export function useSystemStatus() {
  return useQuery({
    queryKey: ["system", "status"],
    queryFn: async () => (await getStatus({ body: {}, throwOnError: true })).data as SystemStatusResponse,
    refetchInterval: 60_000,
  });
}

export type { SystemStatusResponse, OrchestratorControlResponse };

export function useOrchestratorStatus() {
  return useQuery({
    queryKey: ["orchestrator", "status"],
    queryFn: async () =>
      (await orchestratorControl({ body: { action: "status" }, throwOnError: true })).data as OrchestratorControlResponse,
    refetchInterval: 15_000,
  });
}

export function useOrchestratorControl() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (action: "pause" | "resume") =>
      (await orchestratorControl({ body: { action }, throwOnError: true })).data as OrchestratorControlResponse,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["orchestrator", "status"] });
    },
  });
}

// --- Agents ---

export function useAgents(projectId?: string) {
  return useQuery({
    queryKey: ["agents", projectId],
    queryFn: async () => {
      const { data } = await listAgents({
        body: projectId ? { project_id: projectId } : {},
        throwOnError: true,
      });
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
        projectIds.map((pid) => listAgents({ body: { project_id: pid }, throwOnError: true })),
      );
      return results.flatMap((r) => r.data.agents ?? []);
    },
    refetchInterval: 60_000,
    enabled: projectIds.length > 0,
  });
}

// --- Tasks ---

export function useTasks(projectId?: string, opts?: { showAll?: boolean }) {
  return useQuery({
    queryKey: ["tasks", projectId, opts?.showAll],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (projectId) body.project_id = projectId;
      if (opts?.showAll) body.show_all = true;
      const { data } = await listTasks({ body, throwOnError: true });
      return (data as ListTasksResponse).tasks ?? [];
    },
    refetchInterval: 60_000,
  });
}

export function useTask(taskId: string) {
  return useQuery({
    queryKey: ["task", taskId],
    queryFn: async () => (await getTask({ body: { task_id: taskId }, throwOnError: true })).data as TaskResponse,
    refetchInterval: 60_000,
    enabled: !!taskId,
  });
}

export function useActiveTasksAllProjects() {
  return useQuery({
    queryKey: ["tasks", "active", "all"],
    queryFn: async () => {
      const { data } = await listActiveTasksAllProjects({ body: {}, throwOnError: true });
      return (data as ListTasksResponse).tasks ?? [];
    },
    refetchInterval: 60_000,
  });
}

// --- Projects ---

export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const { data } = await listProjects({ body: {}, throwOnError: true });
      return ((data as ListProjectsResponse).projects ?? []).map(withPaused);
    },
    refetchInterval: 30_000,
  });
}

export function useProject(projectId: string) {
  return useQuery({
    queryKey: ["project", projectId],
    queryFn: async () => {
      const { data } = await getProject({ body: { project_id: projectId }, throwOnError: true });
      return withPaused(data as ProjectResponse);
    },
    enabled: !!projectId,
  });
}

function invalidateProjectQueries(queryClient: ReturnType<typeof useQueryClient>, projectId?: string) {
  queryClient.invalidateQueries({ queryKey: ["projects"] });
  if (projectId) queryClient.invalidateQueries({ queryKey: ["project", projectId] });
}

export function usePauseProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { project_id: string }) =>
      (await pauseProject({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateProjectQueries(queryClient, variables.project_id),
  });
}

export function useResumeProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { project_id: string }) =>
      (await resumeProject({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateProjectQueries(queryClient, variables.project_id),
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { project_id: string }) =>
      (await deleteProject({ body: input, throwOnError: true })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

// --- Workspaces ---

export function useWorkspaces(projectId: string) {
  return useQuery({
    queryKey: ["workspaces", projectId],
    queryFn: async () => {
      const { data } = await listWorkspaces({ body: { project_id: projectId }, throwOnError: true });
      return (data as ListWorkspacesResponse).workspaces ?? [];
    },
    refetchInterval: 30_000,
    enabled: !!projectId,
  });
}

// --- Profiles (system-wide) ---

export function useProfiles() {
  return useQuery({
    queryKey: ["profiles"],
    queryFn: async () => {
      const { data } = await listProfiles({ body: {}, throwOnError: true });
      return (data as ListProfilesResponse).profiles ?? [];
    },
    refetchInterval: 60_000,
  });
}

// --- Task Mutations ---

function useTaskMutationCallbacks() {
  const queryClient = useQueryClient();
  return {
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task"] });
    },
  };
}

export function useStopTask() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string }) =>
      (await stopTask({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useRestartTask() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string }) =>
      (await restartTask({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useSkipTask() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string }) =>
      (await skipTask({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useApproveTask() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string }) =>
      (await approveTask({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useApprovePlan() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string }) =>
      (await approvePlan({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useRejectPlan() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string; feedback: string }) =>
      (await rejectPlan({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useDeletePlan() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string }) =>
      (await deletePlan({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useReopenWithFeedback() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string; feedback: string }) =>
      (await reopenWithFeedback({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useEditTask() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: EditTaskRequest) =>
      (await editTask({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useDeleteTask() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string }) =>
      (await deleteTask({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useCreateTask() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: CreateTaskRequest) =>
      (await createTask({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

export function useProvideInput() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string; input: string }) =>
      (await provideInput({ body: input, throwOnError: true })).data,
    ...cb,
  });
}

// --- Playbooks ---

export function usePlaybooks(scope?: string) {
  return useQuery({
    queryKey: ["playbooks", scope ?? "all"],
    queryFn: async () => {
      const { data } = await listPlaybooks({
        body: scope ? { scope } : {},
        throwOnError: true,
      });
      return (data as ListPlaybooksResponse).playbooks ?? [];
    },
    refetchInterval: 30_000,
  });
}

export function usePlaybookSource(playbookId: string) {
  return useQuery({
    queryKey: ["playbook-source", playbookId],
    queryFn: async () =>
      (await getPlaybookSource({ body: { playbook_id: playbookId }, throwOnError: true }))
        .data as GetPlaybookSourceResponse,
    enabled: !!playbookId,
  });
}

export function usePlaybookRuns(playbookId?: string, status?: string, limit = 20) {
  return useQuery({
    queryKey: ["playbook-runs", playbookId ?? "all", status ?? "any", limit],
    queryFn: async () => {
      const body: Record<string, unknown> = { limit };
      if (playbookId) body.playbook_id = playbookId;
      if (status) body.status = status;
      const { data } = await listPlaybookRuns({ body, throwOnError: true });
      return (data as ListPlaybookRunsResponse).runs ?? [];
    },
    refetchInterval: 30_000,
    enabled: !!playbookId || playbookId === undefined,
  });
}

export function useUpdatePlaybookSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { playbook_id: string; markdown: string; expected_source_hash?: string }) =>
      (await updatePlaybookSource({ body: input, throwOnError: true })).data as UpdatePlaybookSourceResponse,
    onSuccess: (_d, variables) => {
      queryClient.invalidateQueries({ queryKey: ["playbook-source", variables.playbook_id] });
      queryClient.invalidateQueries({ queryKey: ["playbooks"] });
    },
  });
}

export function useEventTriggers() {
  return useQuery({
    queryKey: ["event-triggers"],
    queryFn: async () => {
      const { data } = await listEventTriggers({ body: {}, throwOnError: true });
      return (data as ListEventTriggersResponse).events ?? [];
    },
    staleTime: 10 * 60_000,
  });
}

export function useCreatePlaybook() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { playbook_id: string; scope: string; markdown: string }) =>
      (await createPlaybook({ body: input, throwOnError: true })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["playbooks"] });
    },
  });
}

export function useDeletePlaybook() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { playbook_id: string }) =>
      (await deletePlaybook({ body: input, throwOnError: true })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["playbooks"] });
    },
  });
}

// --- Project profiles (per-agent-type override view) ---

export function useProjectProfiles(projectId: string) {
  return useQuery({
    queryKey: ["project-profiles", projectId],
    queryFn: async () => {
      const { data } = await listProjectProfiles({
        body: { project_id: projectId },
        throwOnError: true,
      });
      return data as ListProjectProfilesResponse;
    },
    enabled: !!projectId,
    refetchInterval: 60_000,
  });
}

export function useEffectiveProfile(projectId: string, agentType: string) {
  return useQuery({
    queryKey: ["effective-profile", projectId, agentType],
    queryFn: async () => {
      const { data } = await showEffectiveProfile({
        body: { project_id: projectId, agent_type: agentType },
        throwOnError: true,
      });
      return data as ShowEffectiveProfileResponse;
    },
    enabled: !!projectId && !!agentType,
  });
}

function invalidateProfileViews(queryClient: ReturnType<typeof useQueryClient>, projectId: string) {
  queryClient.invalidateQueries({ queryKey: ["project-profiles", projectId] });
  queryClient.invalidateQueries({ queryKey: ["effective-profile", projectId] });
  queryClient.invalidateQueries({ queryKey: ["profiles"] });
}

export function useCreateProjectProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateProjectProfileRequest) =>
      (await createProjectProfile({ body: input, throwOnError: true })).data as CreateProjectProfileResponse,
    onSuccess: (_d, variables) => invalidateProfileViews(queryClient, variables.project_id),
  });
}

export function useEditProjectProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: EditProjectProfileRequest) =>
      (await editProjectProfile({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateProfileViews(queryClient, variables.project_id),
  });
}

export function useDeleteProjectProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { project_id: string; agent_type: string }) =>
      (await deleteProjectProfile({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateProfileViews(queryClient, variables.project_id),
  });
}

// --- MCP servers (registry) + tool catalog ---

export function useMcpServers(projectId?: string) {
  return useQuery({
    queryKey: ["mcp-servers", projectId ?? "system"],
    queryFn: async () => {
      const { data } = await listMcpServers({
        body: projectId ? { project_id: projectId } : {},
        throwOnError: true,
      });
      return (data as ListMcpServersResponse).servers ?? [];
    },
    refetchInterval: 60_000,
  });
}

export function useMcpServer(name: string, projectId?: string) {
  return useQuery({
    queryKey: ["mcp-server", projectId ?? "system", name],
    queryFn: async () => {
      const { data } = await getMcpServer({
        body: projectId ? { name, project_id: projectId } : { name },
        throwOnError: true,
      });
      return data as GetMcpServerResponse;
    },
    enabled: !!name,
  });
}

export function useToolCatalog(projectId?: string, serverNames?: string[]) {
  return useQuery({
    queryKey: ["tool-catalog", projectId ?? "system", serverNames ?? "all"],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (projectId) body.project_id = projectId;
      if (serverNames && serverNames.length > 0) body.server_names = serverNames;
      const { data } = await listMcpToolCatalog({ body, throwOnError: true });
      return (data as ListMcpToolCatalogResponse).servers ?? {};
    },
    refetchInterval: 60_000,
  });
}

function invalidateMcpViews(queryClient: ReturnType<typeof useQueryClient>, projectId?: string | null) {
  queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
  queryClient.invalidateQueries({ queryKey: ["tool-catalog"] });
  queryClient.invalidateQueries({ queryKey: ["mcp-server"] });
  if (projectId) {
    queryClient.invalidateQueries({ queryKey: ["project-profiles", projectId] });
  }
}

export function useProbeMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { name: string; project_id?: string }) =>
      (await probeMcpServer({ body: input, throwOnError: true })).data as ProbeMcpServerResponse,
    onSuccess: (_d, variables) => invalidateMcpViews(queryClient, variables.project_id),
  });
}

export function useCreateMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateMcpServerRequest) =>
      (await createMcpServer({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateMcpViews(queryClient, variables.project_id),
  });
}

export type EditMcpServerInput = Partial<CreateMcpServerRequest> & {
  name: string;
  project_id?: string;
};

export function useEditMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: EditMcpServerInput) =>
      (await editMcpServer({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateMcpViews(queryClient, variables.project_id),
  });
}

export function useDeleteMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { name: string; project_id?: string }) =>
      (await deleteMcpServer({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateMcpViews(queryClient, variables.project_id),
  });
}
