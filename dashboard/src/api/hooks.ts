import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiGet } from "./legacy-fetch";
import {
  addWorkspace,
  approvePlan,
  approveTask,
  archiveTask,
  createMcpServer,
  createPlaybook,
  createProjectProfile,
  createTask,
  deleteMcpServer,
  deletePlan,
  deletePlaybook,
  deleteProfile,
  deleteProject,
  deleteProjectProfile,
  deleteTask,
  editMcpServer,
  editIntelligenceClass,
  editProfile,
  editProject,
  editProjectProfile,
  editTask,
  editWorkspace,
  getConfig,
  getConfigSchema,
  getMcpServer,
  getProfile,
  getProject,
  getStatus,
  getTask,
  listActiveTasksAllProjects,
  listAgents,
  listIntelligenceClasses,
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
  releaseWorkspace,
  reloadConfig,
  removeWorkspace,
  reopenWithFeedback,
  restartTask,
  resumePlaybook,
  inspectPlaybookRun,
  cancelPlaybookRun,
  resumeProject,
  setPlaybookEnabled,
  showEffectiveProfile,
  skipTask,
  stopTask,
  updateConfig,
  updatePlaybookSource,
  getPlaybookSource,
  sessionList,
  sessionShow,
  sessionPeek,
  sessionNudge,
  sessionAttach,
  sessionLogs,
  sessionKill,
  explainTask,
  projectReady,
  getTaskDependencies,
  gateList,
  gateShow,
  gateResolve,
} from "./client";
import type {
  AgentSummary,
  CatalogEntryModel,
  TaskRef,
  AddWorkspaceRequest,
  CreateMcpServerRequest,
  CreateProjectProfileRequest,
  CreateTaskRequest,
  CreateProjectProfileResponse2 as CreateProjectProfileResponse,
  EditProfileRequest,
  EditIntelligenceClassRequest,
  IntelligenceClassModel,
  EditProjectProfileRequest,
  EditProjectRequest,
  EditTaskRequest,
  EditWorkspaceRequest,
  EventTrigger,
  GetConfigResponse,
  GetConfigSchemaResponse,
  GetMcpServerResponse,
  UpdateConfigResponse,
  ReloadConfigResponse,
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
  ListSessionsResponse,
  ShowSessionResponse,
  SessionPeekResponse,
  SessionNudgeResponse,
  SessionAttachResponse,
  SessionLogsResponse,
  SessionSummary,
  ExplainTaskResponse,
  ProjectReadyResponse,
  TaskDepsResponse,
  GateListResponse,
  GateShowResponse,
  GateResolveResponse,
  GateSummary,
  InspectPlaybookRunResponse,
  CancelPlaybookRunResponse,
} from "./client";
import {
  fetchChatMessages,
  sendChatMessage,
  type ChatMessagesResponse,
} from "./chat";

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

export function useEditProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: EditProjectRequest) =>
      (await editProject({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateProjectQueries(queryClient, variables.project_id),
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

function invalidateWorkspaceViews(
  queryClient: ReturnType<typeof useQueryClient>,
  projectId: string,
) {
  queryClient.invalidateQueries({ queryKey: ["workspaces", projectId] });
  queryClient.invalidateQueries({ queryKey: ["agents", projectId] });
}

export function useAddWorkspace(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: Omit<AddWorkspaceRequest, "project_id">) =>
      (await addWorkspace({
        body: { project_id: projectId, ...input },
        throwOnError: true,
      })).data,
    onSuccess: () => invalidateWorkspaceViews(queryClient, projectId),
  });
}

export function useEditWorkspace(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: EditWorkspaceRequest) =>
      (await editWorkspace({ body: input, throwOnError: true })).data,
    onSuccess: () => invalidateWorkspaceViews(queryClient, projectId),
  });
}

export function useRemoveWorkspace(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { workspace_id: string }) =>
      (await removeWorkspace({
        body: { ...input, project_id: projectId },
        throwOnError: true,
      })).data,
    onSuccess: () => invalidateWorkspaceViews(queryClient, projectId),
  });
}

export function useReleaseWorkspace(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { workspace_id: string }) =>
      (await releaseWorkspace({ body: input, throwOnError: true })).data,
    onSuccess: () => invalidateWorkspaceViews(queryClient, projectId),
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

export function useGetProfile(profileId: string) {
  return useQuery({
    queryKey: ["profile", profileId],
    queryFn: async () => {
      const { data } = await getProfile({
        body: { profile_id: profileId },
        throwOnError: true,
      });
      return data as ProfileDetail;
    },
    enabled: !!profileId,
  });
}

export function useEditProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: EditProfileRequest) =>
      (await editProfile({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => {
      queryClient.invalidateQueries({ queryKey: ["profiles"] });
      queryClient.invalidateQueries({ queryKey: ["profile", variables.profile_id] });
      queryClient.invalidateQueries({ queryKey: ["project-profiles"] });
      queryClient.invalidateQueries({ queryKey: ["effective-profile"] });
    },
  });
}

export function useDeleteProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { profile_id: string }) =>
      (await deleteProfile({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => {
      queryClient.invalidateQueries({ queryKey: ["profiles"] });
      queryClient.invalidateQueries({ queryKey: ["profile", variables.profile_id] });
      queryClient.invalidateQueries({ queryKey: ["project-profiles"] });
      queryClient.invalidateQueries({ queryKey: ["effective-profile"] });
    },
  });
}

export type ProviderSlice = Record<string, unknown>;

export type IntelligenceClassRow = {
  id: string;
  name: string;
  description: string;
  revision: string;
  mapping: Record<string, unknown>;
};

export type IntelligenceClassesResponse = {
  success: boolean;
  classes: IntelligenceClassRow[];
};

function intelligenceClassRow(row: IntelligenceClassModel): IntelligenceClassRow {
  return {
    id: row.id, name: row.name ?? row.id, description: row.description ?? "",
    revision: row.revision ?? "", mapping: row.mapping ?? {},
  };
}

export function useIntelligenceClasses() {
  return useQuery({
    queryKey: ["intelligence-classes"],
    queryFn: async (): Promise<IntelligenceClassesResponse> => {
      const { data } = await listIntelligenceClasses({ body: {}, throwOnError: true });
      return { success: data.success ?? true, classes: (data.classes ?? []).map(intelligenceClassRow) };
    },
    staleTime: 30_000,
  });
}

export function useEditIntelligenceClass() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: EditIntelligenceClassRequest) => {
      const { data } = await editIntelligenceClass({ body: input, throwOnError: true });
      return intelligenceClassRow(data.intelligence_class);
    },
    retry: false,
    onSuccess: (saved) => {
      queryClient.setQueryData<IntelligenceClassesResponse>(["intelligence-classes"], (previous) => previous
        ? { ...previous, classes: previous.classes.map((row) => row.id === saved.id ? saved : row) }
        : undefined);
      void queryClient.invalidateQueries({ queryKey: ["intelligence-classes"] });
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
      void queryClient.invalidateQueries({ queryKey: ["effective-profile"] });
    },
    // Refetch a conflict's latest revision without replacing the editor's draft.
    onError: () => { void queryClient.invalidateQueries({ queryKey: ["intelligence-classes"] }); },
  });
}

// --- Task Mutations ---

function useTaskMutationCallbacks() {
  const queryClient = useQueryClient();
  return {
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task"] });
      queryClient.invalidateQueries({ queryKey: ["projectGraph"] });
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

export function useArchiveTask() {
  const cb = useTaskMutationCallbacks();
  return useMutation({
    mutationFn: async (input: { task_id: string }) =>
      (await archiveTask({ body: input, throwOnError: true })).data,
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

/**
 * Toggle a playbook *definition's* `enabled` flag — pauses/resumes whether
 * trigger events spawn new runs. Distinct from useResumePlaybookRun, which
 * resumes a single in-flight run that's waiting on human input.
 */
export function useSetPlaybookEnabled() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { playbook_id: string; enabled: boolean }) =>
      (await setPlaybookEnabled({ body: input, throwOnError: true })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["playbooks"] });
    },
  });
}

/**
 * Resume a paused playbook **run** (a single in-flight instance waiting on
 * human input). NOT the same as toggling a playbook definition's `enabled`
 * field — that's a separate Phase 2 concern handled via setPlaybookEnabled.
 */
export function useResumePlaybookRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { run_id: string; human_input: string }) =>
      (await resumePlaybook({ body: input, throwOnError: true })).data,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["playbook-runs"] });
      queryClient.invalidateQueries({ queryKey: ["playbook-run", variables.run_id] });
    },
  });
}

/**
 * Inspect a single playbook run — full node trace, conversation history,
 * and HITL/event-wait state. Powers the playbook-run-inspector pane.
 *
 * Polls every 4s while the run is `running`, since node-level bus events
 * (`notify.playbook_run_node_started/_completed`) exist but the pane's WS
 * subscription only invalidates on run-level transitions today — the poll
 * is the fallback for node-level liveness (pane spec §7.4).
 */
export function useInspectPlaybookRun(runId: string | undefined) {
  return useQuery({
    queryKey: ["playbook-run", runId],
    queryFn: async () => {
      const { data } = await inspectPlaybookRun({
        body: { run_id: runId! },
        throwOnError: true,
      });
      return data as InspectPlaybookRunResponse;
    },
    enabled: !!runId,
    refetchInterval: (query) => (query.state.data?.status === "running" ? 4_000 : false),
  });
}

/**
 * Cancel a playbook run that is running or paused. Distinct from
 * useResumePlaybookRun (continues a paused run) and useSetPlaybookEnabled
 * (toggles a playbook *definition*, not a run).
 */
export function useCancelPlaybookRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { run_id: string }) =>
      (await cancelPlaybookRun({ body: input, throwOnError: true }))
        .data as CancelPlaybookRunResponse,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["playbook-runs"] });
      queryClient.invalidateQueries({ queryKey: ["playbook-run", variables.run_id] });
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

// --- Explain / Ready / Task deps (work-graph WG-4) ---

export type { ExplainTaskResponse, ProjectReadyResponse, TaskDepsResponse };

export function useExplainTask(taskId: string) {
  return useQuery({
    queryKey: ["explain", taskId],
    queryFn: async () => {
      const { data } = await explainTask({
        body: { task_id: taskId },
        throwOnError: true,
      });
      return data as ExplainTaskResponse;
    },
    enabled: !!taskId,
    refetchInterval: 20_000,
  });
}

export function useProjectReady(projectId: string) {
  return useQuery({
    queryKey: ["project-ready", projectId],
    queryFn: async () => {
      const { data } = await projectReady({
        body: { project_id: projectId },
        throwOnError: true,
      });
      return data as ProjectReadyResponse;
    },
    enabled: !!projectId,
    refetchInterval: 30_000,
  });
}

export function useTaskDeps(taskId: string) {
  return useQuery({
    queryKey: ["task-deps", taskId],
    queryFn: async () => {
      const { data } = await getTaskDependencies({
        body: { task_id: taskId },
        throwOnError: true,
      });
      return data as TaskDepsResponse;
    },
    enabled: !!taskId,
    refetchInterval: 30_000,
  });
}

// --- Sessions (session-runtime spec) ---

export type { SessionSummary, ListSessionsResponse, SessionLogsResponse };

export function useSessions(projectId?: string) {
  return useQuery({
    queryKey: ["sessions", projectId ?? "all"],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (projectId) body.project_id = projectId;
      const { data } = await sessionList({ body, throwOnError: true });
      return ((data as ListSessionsResponse).sessions ?? []) as SessionSummary[];
    },
    refetchInterval: 15_000,
  });
}

export function useSession(sessionId: string) {
  return useQuery({
    queryKey: ["session", sessionId],
    queryFn: async () => {
      const { data } = await sessionShow({
        body: { session_id: sessionId },
        throwOnError: true,
      });
      return (data as ShowSessionResponse).session;
    },
    enabled: !!sessionId,
    refetchInterval: 15_000,
  });
}

export function useSessionPeek(sessionId: string, lines = 120) {
  return useQuery({
    queryKey: ["session-peek", sessionId, lines],
    queryFn: async () => {
      const { data } = await sessionPeek({
        body: { session_id: sessionId, lines },
        throwOnError: true,
      });
      return data as SessionPeekResponse;
    },
    enabled: !!sessionId,
    refetchInterval: 10_000,
  });
}

export function useSessionAttach(sessionId: string) {
  return useQuery({
    queryKey: ["session-attach", sessionId],
    queryFn: async () => {
      const { data } = await sessionAttach({
        body: { session_id: sessionId },
        throwOnError: true,
      });
      return data as SessionAttachResponse;
    },
    enabled: !!sessionId,
    staleTime: 60_000,
  });
}

function invalidateSessionViews(
  queryClient: ReturnType<typeof useQueryClient>,
  sessionId?: string,
) {
  queryClient.invalidateQueries({ queryKey: ["sessions"] });
  if (sessionId) {
    queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-peek", sessionId] });
  }
}

export function useSessionNudge() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { session_id: string; text: string }) =>
      (await sessionNudge({ body: input, throwOnError: true })).data as SessionNudgeResponse,
    onSuccess: (_d, variables) => invalidateSessionViews(queryClient, variables.session_id),
  });
}

export function useSessionKill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { session_id: string }) =>
      (await sessionKill({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateSessionViews(queryClient, variables.session_id),
  });
}

export function useSessionLogs(sessionId: string, limit = 200) {
  return useQuery({
    queryKey: ["session-logs", sessionId, limit],
    queryFn: async () => {
      const { data } = await sessionLogs({
        body: { session_id: sessionId, limit },
        throwOnError: true,
      });
      return data as SessionLogsResponse;
    },
    enabled: !!sessionId,
  });
}

// --- Gates (work-graph WG-3) ---

export type { GateSummary, GateListResponse };

export function useGates(
  opts: { projectId?: string; status?: string; gateType?: string; enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: [
      "gates",
      opts.projectId ?? "all",
      opts.status ?? "any",
      opts.gateType ?? "any",
    ],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (opts.projectId) body.project_id = opts.projectId;
      if (opts.status) body.status = opts.status;
      if (opts.gateType) body.gate_type = opts.gateType;
      const { data } = await gateList({ body, throwOnError: true });
      return ((data as GateListResponse).gates ?? []) as GateSummary[];
    },
    refetchInterval: 20_000,
    enabled: opts.enabled ?? true,
  });
}

/**
 * Every open gate across every project. Backs the shell TopBar badge and the
 * Activity drawer Gates tab. Fans out one gateList per project because the
 * daemon's list endpoint is project-scoped.
 */
export function useAllOpenGates() {
  const { data: projects } = useProjects();
  const ids = (projects ?? []).map((p) => p.id);
  return useQuery({
    queryKey: ["gates", "open", "all", ids],
    queryFn: async () => {
      if (ids.length === 0) return [] as GateSummary[];
      const results = await Promise.all(
        ids.map((pid) =>
          gateList({
            body: { project_id: pid, status: "open" },
            throwOnError: true,
          }),
        ),
      );
      return results.flatMap(
        (r) => ((r.data as GateListResponse).gates ?? []) as GateSummary[],
      );
    },
    refetchInterval: 20_000,
    enabled: ids.length > 0,
  });
}

export function useGate(gateId: string) {
  return useQuery({
    queryKey: ["gate", gateId],
    queryFn: async () => {
      const { data } = await gateShow({
        body: { gate_id: gateId },
        throwOnError: true,
      });
      return data as GateShowResponse;
    },
    enabled: !!gateId,
  });
}

export function useResolveGate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      gate_id: string;
      resolved_by: string;
      resolution?: string;
    }) =>
      (await gateResolve({ body: input, throwOnError: true })).data as GateResolveResponse,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["gates"] });
      queryClient.invalidateQueries({ queryKey: ["gate", variables.gate_id] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

// --- Chat (supervisor per-project chat, supervisor-agent §6.1) ---

export type { ChatMessagesResponse };

export function useChatMessages(projectId: string, limit = 200) {
  return useQuery({
    queryKey: ["chat", projectId, limit],
    queryFn: () => fetchChatMessages(projectId, { limit }),
    enabled: !!projectId,
    refetchInterval: 15_000,
  });
}

export function useSendChatMessage(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: string) =>
      sendChatMessage(projectId, body, { threadId: `dashboard:${projectId}` }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat", projectId] });
    },
  });
}

// --- System config (~/.agent-queue/config.yaml editor) ---

export function useSystemConfig() {
  return useQuery({
    queryKey: ["system-config"],
    queryFn: async () => {
      const { data } = await getConfig({ body: {}, throwOnError: true });
      return data as GetConfigResponse;
    },
  });
}

export function useSystemConfigSchema() {
  return useQuery({
    queryKey: ["system-config-schema"],
    queryFn: async () => {
      const { data } = await getConfigSchema({ body: {}, throwOnError: true });
      return data as GetConfigSchemaResponse;
    },
    // Schema is derived from code — only changes between deploys.
    staleTime: 5 * 60_000,
  });
}

export function useUpdateSystemConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { section: string; data: unknown; dry_run?: boolean }) =>
      // The generated type narrows `data` to an object, but the API also accepts
      // arrays, scalars, and null. Cast at the boundary.
      (await updateConfig({ body: input as never, throwOnError: true }))
        .data as UpdateConfigResponse,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["system-config"] });
    },
  });
}

export function useReloadSystemConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      (await reloadConfig({ body: {}, throwOnError: true })).data as ReloadConfigResponse,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["system-config"] });
    },
  });
}
