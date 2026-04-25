import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import SystemOverview from "./pages/system/Overview";
import SystemEvents from "./pages/system/Events";
import SystemPlaybooks from "./pages/system/Playbooks";
import SystemConfig from "./pages/system/Config";
import ProjectLayout from "./pages/project/ProjectLayout";
import ProjectOverview from "./pages/project/Overview";
import ProjectTasks from "./pages/project/Tasks";
import ProjectWorkspaces from "./pages/project/Workspaces";
import ProjectProfiles from "./pages/project/Profiles";
import ProjectPlaybooks from "./pages/project/Playbooks";
import ProjectConfig from "./pages/project/Config";
import TaskDetail from "./pages/TaskDetail";
import PlaybookDetail from "./pages/PlaybookDetail";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/system" replace />} />

        <Route path="system">
          <Route index element={<SystemOverview />} />
          <Route path="events" element={<SystemEvents />} />
          <Route path="playbooks" element={<SystemPlaybooks />} />
          <Route path="config" element={<SystemConfig />} />
        </Route>

        <Route path="projects/:projectId" element={<ProjectLayout />}>
          <Route index element={<ProjectOverview />} />
          <Route path="tasks" element={<ProjectTasks />} />
          <Route path="workspaces" element={<ProjectWorkspaces />} />
          <Route path="profiles" element={<ProjectProfiles />} />
          <Route path="playbooks" element={<ProjectPlaybooks />} />
          <Route path="config" element={<ProjectConfig />} />
        </Route>

        <Route path="tasks/:taskId" element={<TaskDetail />} />
        <Route path="playbooks/:playbookId" element={<PlaybookDetail />} />

        {/* Legacy redirects */}
        <Route path="agents" element={<Navigate to="/system" replace />} />
        <Route path="tasks" element={<Navigate to="/system" replace />} />
        <Route path="playbooks" element={<Navigate to="/system/playbooks" replace />} />
        <Route path="events" element={<Navigate to="/system/events" replace />} />

        <Route path="*" element={<Navigate to="/system" replace />} />
      </Route>
    </Routes>
  );
}
