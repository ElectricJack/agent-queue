import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";

import ChatLanding from "./pages/chat/ChatLanding";
import ChatConversation from "./pages/chat/ChatConversation";
import CommandCenterPlaceholder from "./pages/command-center/CommandCenterPlaceholder";

import WorkIndex from "./pages/work/WorkIndex";

import SettingsLayout from "./pages/settings/SettingsLayout";
import SystemPlaybooks from "./pages/system/Playbooks";
import SystemProfiles from "./pages/system/Profiles";
import SystemConfig from "./pages/system/Config";
import IntelligenceClassesStub from "./pages/settings/IntelligenceClassesStub";

import SystemEvents from "./pages/system/Events";
import SystemSessions from "./pages/system/Sessions";
import SystemGates from "./pages/system/Gates";

import ProjectLayout from "./pages/project/ProjectLayout";
import ProjectOverview from "./pages/project/Overview";
import ProjectTasks from "./pages/project/Tasks";
import ProjectWorkspaces from "./pages/project/Workspaces";
import ProjectProfiles from "./pages/project/Profiles";
import ProjectPlaybooks from "./pages/project/Playbooks";
import ProjectConfig from "./pages/project/Config";
import ProjectSessions from "./pages/project/Sessions";
import ProjectChat from "./pages/project/Chat";

import TaskDetail from "./pages/TaskDetail";
import PlaybookDetail from "./pages/PlaybookDetail";
import SessionDetail from "./pages/SessionDetail";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        {/* Top-level IA — Phase 3 */}
        <Route index element={<ChatLanding />} />
        <Route path="chat/:projectId" element={<ChatConversation />} />
        <Route path="command-center" element={<CommandCenterPlaceholder />} />
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
          <Route path="chat" element={<ProjectChat />} />
          <Route path="workspaces" element={<ProjectWorkspaces />} />
          <Route path="profiles" element={<ProjectProfiles />} />
          <Route path="playbooks" element={<ProjectPlaybooks />} />
          <Route path="config" element={<ProjectConfig />} />
        </Route>

        {/* Detail routes unchanged */}
        <Route path="tasks/:taskId" element={<TaskDetail />} />
        <Route path="sessions/:sessionId" element={<SessionDetail />} />
        <Route path="playbooks/:playbookId" element={<PlaybookDetail />} />

        {/* Legacy redirects — the four Phase-2 nav entries + old aliases */}
        <Route path="system" element={<Navigate to="/work" replace />} />
        <Route path="system/events" element={<Navigate to="/work/events" replace />} />
        <Route path="system/sessions" element={<Navigate to="/work/sessions" replace />} />
        <Route path="system/gates" element={<Navigate to="/work/gates" replace />} />
        <Route path="system/playbooks" element={<Navigate to="/settings/playbooks" replace />} />
        <Route path="system/profiles" element={<Navigate to="/settings/profiles" replace />} />
        <Route path="system/config" element={<Navigate to="/settings/config" replace />} />
        <Route path="agents" element={<Navigate to="/work" replace />} />
        <Route path="tasks" element={<Navigate to="/work" replace />} />
        <Route path="playbooks" element={<Navigate to="/settings/playbooks" replace />} />
        <Route path="events" element={<Navigate to="/work/events" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
