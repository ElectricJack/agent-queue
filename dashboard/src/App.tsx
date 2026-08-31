import { lazy, Suspense, useEffect, useRef } from "react";
import { Routes, Route, Navigate, useLocation, useParams } from "react-router-dom";
import { ShellPaneProvider, useShellPaneStore } from "./panes/store";
import { projectNavigation, workspaceHref } from "./shell/projectNavigation";

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
const ProjectProfiles = lazy(() => import("./pages/project/Profiles"));
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

/** Spans both route families so switching to All projects also clears selection. */
function ProjectScopePaneSync() {
  const { pathname } = useLocation();
  const { projectId } = projectNavigation(pathname);
  const previousProject = useRef(projectId);
  const pane = useShellPaneStore();
  useEffect(() => {
    if (previousProject.current !== projectId) {
      previousProject.current = projectId;
      if (pane.state.kind === "open" && pane.state.view === "task-detail") pane.close();
    }
  }, [projectId, pane]);
  return null;
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

            <Route path="command-center" element={<CommandCenter />}>
              <Route index element={<WorkspaceIndexRedirect />} />
              <Route path="graph" element={<CommandCenterGraph />} />
              <Route path="tasks" element={<CommandCenterTasks />} />
              <Route path="agents" element={<Navigate to="/agents" replace />} />
            </Route>

            {/* Legacy /work* — kept as redirects for external deep-links. */}
            <Route path="work" element={<Navigate to="/command-center/tasks" replace />} />
            <Route path="work/tasks" element={<Navigate to="/command-center/tasks" replace />} />
            <Route path="work/agents" element={<Navigate to="/agents" replace />} />
            <Route path="work/sessions" element={<Navigate to="/agents" replace />} />
            <Route
              path="work/events"
              element={<Navigate to="/command-center/tasks?openDrawer=events" replace />}
            />
            <Route
              path="work/gates"
              element={<Navigate to="/command-center/tasks?openDrawer=gates" replace />}
            />

            <Route path="settings" element={<SettingsLayout />}>
              <Route index element={<Navigate to="playbooks" replace />} />
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
              <Route path="profiles" element={<ProjectProfiles />} />
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
