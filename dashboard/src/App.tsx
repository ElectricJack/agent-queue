import { lazy, Suspense } from "react";
import { Routes, Route, Navigate, useParams } from "react-router-dom";
import Layout from "./components/Layout";
import { useIsV2 } from "./shell/useIsV2";

const AppShellV2 = lazy(() => import("./shell/AppShellV2"));
const GlobalChat = lazy(() => import("./pages/GlobalChat"));
const CommandCenterGraph = lazy(() => import("./pages/command-center/Graph"));
const CommandCenterTasks = lazy(() => import("./pages/command-center/Tasks"));
const CommandCenterAgents = lazy(() => import("./pages/command-center/Agents"));

const ChatLanding = lazy(() => import("./pages/chat/ChatLanding"));
const ChatConversation = lazy(() => import("./pages/chat/ChatConversation"));
const CommandCenter = lazy(() => import("./pages/CommandCenter"));

const WorkIndex = lazy(() => import("./pages/work/WorkIndex"));

const SettingsLayout = lazy(() => import("./pages/settings/SettingsLayout"));
const SystemPlaybooks = lazy(() => import("./pages/system/Playbooks"));
const SystemProfiles = lazy(() => import("./pages/system/Profiles"));
const SystemConfig = lazy(() => import("./pages/system/Config"));
const IntelligenceClassesStub = lazy(() => import("./pages/settings/IntelligenceClassesStub"));

const SystemEvents = lazy(() => import("./pages/system/Events"));
const SystemSessions = lazy(() => import("./pages/system/Sessions"));
const SystemGates = lazy(() => import("./pages/system/Gates"));

const ProjectLayout = lazy(() => import("./pages/project/ProjectLayout"));
const ProjectOverview = lazy(() => import("./pages/project/Overview"));
const ProjectTasks = lazy(() => import("./pages/project/Tasks"));
const ProjectWorkspaces = lazy(() => import("./pages/project/Workspaces"));
const ProjectProfiles = lazy(() => import("./pages/project/Profiles"));
const ProjectPlaybooks = lazy(() => import("./pages/project/Playbooks"));
const ProjectConfig = lazy(() => import("./pages/project/Config"));
const ProjectSessions = lazy(() => import("./pages/project/Sessions"));

const TaskDetail = lazy(() => import("./pages/TaskDetail"));
const PlaybookDetail = lazy(() => import("./pages/PlaybookDetail"));
const SessionDetail = lazy(() => import("./pages/SessionDetail"));
const TaskFiles = lazy(() => import("./pages/TaskFiles"));

/** `/chat/:projectId` (Phase 3, matches the top-level IA) is the canonical
 *  project-chat surface — redirect this older project-scoped alias there
 *  rather than maintaining two chat UIs. */
function ProjectChatRedirect() {
  const { projectId = "" } = useParams();
  return <Navigate to={`/chat/${projectId}`} replace />;
}

function RouteFallback() {
  return (
    <div className="flex h-full min-h-[40vh] items-center justify-center text-sm text-gray-500">
      Loading…
    </div>
  );
}

function AppV1() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route element={<Layout />}>
          {/* Top-level IA — Phase 3 */}
          <Route index element={<ChatLanding />} />
          <Route path="chat/:projectId" element={<ChatConversation />} />
          <Route path="command-center" element={<CommandCenter />}>
            <Route index element={<Navigate to="graph" replace />} />
            <Route path="graph" element={<CommandCenterGraph />} />
            <Route path="tasks" element={<CommandCenterTasks />} />
            <Route path="agents" element={<CommandCenterAgents />} />
          </Route>
          <Route path="work" element={<WorkIndex />} />

          {/* Settings hub — playbooks / profiles / intelligence-classes / config */}
          <Route path="settings" element={<SettingsLayout />}>
            <Route index element={<Navigate to="playbooks" replace />} />
            <Route path="playbooks" element={<SystemPlaybooks />} />
            <Route path="profiles" element={<SystemProfiles />} />
            <Route path="intelligence-classes" element={<IntelligenceClassesStub />} />
            <Route path="config" element={<SystemConfig />} />
          </Route>

          {/* Existing sub-surfaces kept reachable (Events/Sessions/Gates as sub-tabs of Work) */}
          <Route path="work/events" element={<SystemEvents />} />
          <Route path="work/sessions" element={<SystemSessions />} />
          <Route path="work/gates" element={<SystemGates />} />

          {/* Project surfaces stay intact for deep-links */}
          <Route path="projects/:projectId" element={<ProjectLayout />}>
            <Route index element={<ProjectOverview />} />
            <Route path="tasks" element={<ProjectTasks />} />
            <Route path="sessions" element={<ProjectSessions />} />
            <Route path="chat" element={<ProjectChatRedirect />} />
            <Route path="workspaces" element={<ProjectWorkspaces />} />
            <Route path="profiles" element={<ProjectProfiles />} />
            <Route path="playbooks" element={<ProjectPlaybooks />} />
            <Route path="config" element={<ProjectConfig />} />
          </Route>

          {/* Detail routes unchanged */}
          <Route path="tasks/:taskId" element={<TaskDetail />} />
          <Route path="tasks/:taskId/files" element={<TaskFiles />} />
          <Route path="sessions/:sessionId" element={<SessionDetail />} />
          <Route path="playbooks/:playbookId" element={<PlaybookDetail />} />

          {/* Legacy redirects — the four Phase-2 nav entries + old aliases */}
          <Route path="system" element={<Navigate to="/work" replace />} />
          <Route path="system/events" element={<Navigate to="/work/events" replace />} />
          <Route path="system/sessions" element={<Navigate to="/work/sessions" replace />} />
          <Route path="system/gates" element={<Navigate to="/work/gates" replace />} />
          <Route
            path="system/playbooks"
            element={<Navigate to="/settings/playbooks" replace />}
          />
          <Route
            path="system/profiles"
            element={<Navigate to="/settings/profiles" replace />}
          />
          <Route path="system/config" element={<Navigate to="/settings/config" replace />} />
          <Route path="agents" element={<Navigate to="/work" replace />} />
          <Route path="tasks" element={<Navigate to="/work" replace />} />
          <Route path="playbooks" element={<Navigate to="/settings/playbooks" replace />} />
          <Route path="events" element={<Navigate to="/work/events" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}

function AppV2() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route element={<AppShellV2 />}>
          {/* / is the global Agent Q supervisor chat. */}
          <Route index element={<GlobalChat />} />
          <Route path="chat/:projectId" element={<ChatConversation />} />

          <Route path="command-center" element={<CommandCenter />}>
            <Route index element={<Navigate to="graph?v2=1" replace />} />
            <Route path="graph" element={<CommandCenterGraph />} />
            <Route path="tasks" element={<CommandCenterTasks />} />
            <Route path="agents" element={<CommandCenterAgents />} />
          </Route>

          {/* Legacy /work* — kept as redirects until v1 shell removal. */}
          <Route
            path="work"
            element={<Navigate to="/command-center/tasks?v2=1" replace />}
          />
          <Route
            path="work/tasks"
            element={<Navigate to="/command-center/tasks?v2=1" replace />}
          />
          <Route
            path="work/agents"
            element={<Navigate to="/command-center/agents?v2=1" replace />}
          />
          <Route
            path="work/sessions"
            element={<Navigate to="/command-center/agents?v2=1" replace />}
          />
          <Route
            path="work/events"
            element={
              <Navigate to="/command-center/tasks?v2=1&openDrawer=events" replace />
            }
          />
          <Route
            path="work/gates"
            element={
              <Navigate to="/command-center/tasks?v2=1&openDrawer=gates" replace />
            }
          />

          <Route path="settings" element={<SettingsLayout />}>
            <Route index element={<Navigate to="playbooks" replace />} />
            <Route path="playbooks" element={<SystemPlaybooks />} />
            <Route path="profiles" element={<SystemProfiles />} />
            <Route
              path="intelligence-classes"
              element={<IntelligenceClassesStub />}
            />
            <Route path="config" element={<SystemConfig />} />
          </Route>

          <Route path="projects/:projectId" element={<ProjectLayout />}>
            <Route index element={<ProjectOverview />} />
            <Route path="tasks" element={<ProjectTasks />} />
            <Route path="sessions" element={<ProjectSessions />} />
            <Route path="chat" element={<ProjectChatRedirect />} />
            <Route path="workspaces" element={<ProjectWorkspaces />} />
            <Route path="profiles" element={<ProjectProfiles />} />
            <Route path="playbooks" element={<ProjectPlaybooks />} />
            <Route path="config" element={<ProjectConfig />} />
          </Route>

          <Route path="tasks/:taskId" element={<TaskDetail />} />
          <Route path="tasks/:taskId/files" element={<TaskFiles />} />
          <Route path="sessions/:sessionId" element={<SessionDetail />} />
          <Route path="playbooks/:playbookId" element={<PlaybookDetail />} />

          <Route path="*" element={<Navigate to="/?v2=1" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}

export default function App() {
  const v2 = useIsV2();
  return v2 ? <AppV2 /> : <AppV1 />;
}
