import { lazy, Suspense, useEffect, useRef } from "react";
import { Routes, Route, Navigate, Link, useLocation, useParams, type Params } from "react-router-dom";
import { ShellPaneProvider, useShellPaneStore } from "./panes/store";
import { projectNavigation, workspaceHref } from "./shell/projectNavigation";
import { useProjects } from "./api/hooks";

const AppShellV2 = lazy(() => import("./shell/AppShellV2"));
const AgentWorkspace = lazy(() => import("./pages/agents/AgentWorkspace"));
const CommandCenterGraph = lazy(() => import("./pages/command-center/Graph"));
const CommandCenterTasks = lazy(() => import("./pages/command-center/Tasks"));

const CommandCenter = lazy(() => import("./pages/CommandCenter"));

const SettingsLayout = lazy(() => import("./pages/settings/SettingsLayout"));
const SystemPlaybooks = lazy(() => import("./pages/system/Playbooks"));
const SystemProfiles = lazy(() => import("./pages/system/Profiles"));
const SystemConfig = lazy(() => import("./pages/system/Config"));
const IntelligenceClassesStub = lazy(() => import("./pages/settings/IntelligenceClassesStub"));

const ProjectOverview = lazy(() => import("./pages/project/Overview"));
const ProjectWorkspaces = lazy(() => import("./pages/project/Workspaces"));
const ProjectPlaybooks = lazy(() => import("./pages/project/Playbooks"));
const ProjectConfig = lazy(() => import("./pages/project/Config"));
const ProjectSessions = lazy(() => import("./pages/project/Sessions"));

const TaskDetail = lazy(() => import("./pages/TaskDetail"));
const PlaybookDetail = lazy(() => import("./pages/PlaybookDetail"));
const SessionDetail = lazy(() => import("./pages/SessionDetail"));
const TaskFiles = lazy(() => import("./pages/TaskFiles"));

/** Index redirects must retain the shared URL-backed task filters. */
function WorkspaceIndexRedirect() {
  const { projectId } = useParams();
  const { search } = useLocation();
  return <Navigate to={workspaceHref(projectId, "graph", search)} replace />;
}

/** Tracks project scope and clears task panes when switching projects. */
function ProjectScopePaneSync() {
  const location = useLocation();
  const { projectId } = projectNavigation(location.pathname);
  const restoreTaskId = (location.state as { restoreTaskPane?: { taskId?: string } } | null)?.restoreTaskPane?.taskId;
  const restoredLocation = useRef<string | null>(null);
  const previousProject = useRef(projectId);
  const pane = useShellPaneStore();
  useEffect(() => {
    if (projectId) {
      try { window.localStorage.setItem(LAST_PROJECT_STORAGE_KEY, projectId); } catch { /* Storage may be unavailable. */ }
    }
    if (previousProject.current !== projectId) {
      previousProject.current = projectId;
      if (pane.state.kind === "open" && pane.state.view === "task-detail") pane.close();
    }
    // Session history preserves pane context separately from the exact URL.
    // Restore after project-scope cleanup and only once per return navigation.
    if (restoreTaskId && restoredLocation.current !== location.key) {
      restoredLocation.current = location.key;
      pane.open("task-detail", { taskId: restoreTaskId });
    }
  }, [projectId, pane, restoreTaskId, location.key]);
  return null;
}

type RedirectTarget = string | ((params: Readonly<Params<string>>) => string);

function withPreservedSearch(destination: string, search: string): string {
  const [pathname, destinationSearch = ""] = destination.split("?", 2);
  const params = new URLSearchParams(search);
  new URLSearchParams(destinationSearch).forEach((value, key) => params.set(key, value));
  const nextSearch = params.toString();
  return pathname + (nextSearch ? "?" + nextSearch : "");
}

function LegacyRedirect({ to }: { to: RedirectTarget }) {
  const location = useLocation();
  const params = useParams();
  const destination = typeof to === "function" ? to(params) : to;
  return <Navigate to={withPreservedSearch(destination, location.search)} replace />;
}

function encodePlaybookParam(params: Readonly<Params<string>>): string {
  return "/playbooks/" + encodeURIComponent(params.playbookId ?? "");
}

const LAST_PROJECT_STORAGE_KEY = "aq.dashboard.lastProjectId";

function readRememberedProjectId(): string | null {
  try {
    return window.localStorage.getItem(LAST_PROJECT_STORAGE_KEY);
  } catch {
    return null;
  }
}

function NoProjectsState() {
  return (
    <div className="flex h-full min-h-[40vh] items-center justify-center p-6">
      <div className="max-w-md rounded-xl border border-gray-800 bg-gray-900/70 p-8 text-center">
        <h1 className="text-xl font-semibold text-gray-100">No projects yet</h1>
        <p className="mt-2 text-sm text-gray-400">
          Add a project to start using Command Center.
        </p>
        <Link
          to="/settings"
          className="mt-5 inline-flex rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500"
        >
          Add project
        </Link>
      </div>
    </div>
  );
}

function CommandCenterRedirect() {
  const { data: projects, isLoading } = useProjects();
  const location = useLocation();
  if (isLoading || !projects) return <RouteFallback />;
  if (projects.length === 0) return <NoProjectsState />;

  const requestedTab = location.pathname
    .slice("/command-center".length)
    .split("/")
    .filter(Boolean)[0];
  const tab = requestedTab === "tasks" ? "tasks" : "graph";
  const remembered = readRememberedProjectId();
  const project = projects.find((candidate) => candidate.id === remembered) ?? projects[0];
  if (!project) return <NoProjectsState />;
  return <Navigate to={workspaceHref(project.id, tab, location.search)} replace />;
}

function RouteFallback() {
  return (
    <div className="flex h-full min-h-[40vh] items-center justify-center text-sm text-gray-500">
      Loading…
    </div>
  );
}

export default function App() {
  return (
    <ShellPaneProvider>
      <ProjectScopePaneSync />
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route element={<AppShellV2 />}>
            <Route index element={<Navigate to="/command-center" replace />} />
            <Route path="agents" element={<AgentWorkspace />} />
            <Route path="chat/:projectId" element={<Navigate to="/agents" replace />} />

            <Route path="command-center/agents" element={<Navigate to="/agents" replace />} />
            <Route path="command-center/*" element={<CommandCenterRedirect />} />

            {/* Legacy deep-links retain their filters while moving to current surfaces. */}
            <Route path="system" element={<LegacyRedirect to="/command-center/graph" />} />
            <Route path="system/events" element={<LegacyRedirect to="/command-center/tasks?openDrawer=events" />} />
            <Route path="system/gates" element={<LegacyRedirect to="/command-center/tasks?openDrawer=gates" />} />
            <Route path="system/playbooks" element={<LegacyRedirect to="/settings/playbooks" />} />
            <Route path="system/profiles" element={<LegacyRedirect to="/settings/profiles" />} />
            <Route path="system/config" element={<LegacyRedirect to="/settings/config" />} />
            <Route path="system/intelligence-classes" element={<LegacyRedirect to="/settings/intelligence-classes" />} />
            <Route path="tasks" element={<LegacyRedirect to="/command-center/tasks" />} />
            <Route path="playbooks" element={<LegacyRedirect to="/settings/playbooks" />} />
            <Route path="work" element={<LegacyRedirect to="/command-center/tasks" />} />
            <Route path="work/tasks" element={<LegacyRedirect to="/command-center/tasks" />} />
            <Route path="work/agents" element={<LegacyRedirect to="/agents" />} />
            <Route path="work/sessions" element={<LegacyRedirect to="/agents" />} />
            <Route path="work/events" element={<LegacyRedirect to="/command-center/tasks?openDrawer=events" />} />
            <Route path="work/gates" element={<LegacyRedirect to="/command-center/tasks?openDrawer=gates" />} />

            <Route path="settings" element={<SettingsLayout />}>
              <Route index element={<Navigate to="playbooks" replace />} />
              <Route path="playbooks/:playbookId" element={<LegacyRedirect to={encodePlaybookParam} />} />
              <Route path="playbooks" element={<SystemPlaybooks />} />
              <Route path="profiles" element={<SystemProfiles />} />
              <Route path="intelligence-classes" element={<IntelligenceClassesStub />} />
              <Route path="config" element={<SystemConfig />} />
            </Route>

            <Route path="projects/:projectId" element={<CommandCenter />}>
              <Route index element={<WorkspaceIndexRedirect />} />
              <Route path="graph" element={<CommandCenterGraph />} />
              <Route path="tasks" element={<CommandCenterTasks />} />
              <Route path="overview" element={<ProjectOverview />} />
              <Route path="sessions" element={<ProjectSessions />} />
              <Route path="chat" element={<Navigate to="/agents" replace />} />
              <Route path="workspaces" element={<ProjectWorkspaces />} />
              <Route path="profiles" element={<LegacyRedirect to={(params) => `/projects/${encodeURIComponent(params.projectId ?? "")}/config`} />} />
              <Route path="playbooks" element={<ProjectPlaybooks />} />
              <Route path="config" element={<ProjectConfig />} />
            </Route>

            <Route path="tasks/:taskId" element={<TaskDetail />} />
            <Route path="tasks/:taskId/files" element={<TaskFiles />} />
            <Route path="sessions/:sessionId" element={<SessionDetail />} />
            <Route path="playbooks/:playbookId" element={<PlaybookDetail />} />

            <Route path="*" element={<Navigate to="/command-center" replace />} />
          </Route>
        </Routes>
      </Suspense>
    </ShellPaneProvider>
  );
}
