import { NavLink, Outlet, useParams } from "react-router-dom";
import { useProject } from "../../api/hooks";

const tabs = [
  { to: ".", label: "Overview", end: true },
  { to: "tasks", label: "Tasks" },
  { to: "workspaces", label: "Workspaces" },
  { to: "profiles", label: "Profiles" },
  { to: "playbooks", label: "Playbooks" },
  { to: "config", label: "Config" },
] as const;

export default function ProjectLayout() {
  const { projectId = "" } = useParams();
  const { data: project, isLoading } = useProject(projectId);

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Project</p>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold">
            {isLoading ? projectId : project?.name || projectId}
          </h1>
          {project?.paused && (
            <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-400">
              paused
            </span>
          )}
        </div>
        {project?.repo_path && (
          <p className="font-mono text-xs text-gray-500">{project.repo_path}</p>
        )}
      </header>

      <div className="flex gap-1 border-b border-gray-800">
        {tabs.map(({ to, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `px-4 py-2 text-sm font-medium transition-colors ${
                isActive
                  ? "border-b-2 border-indigo-400 text-indigo-400"
                  : "text-gray-400 hover:text-gray-200"
              }`
            }
          >
            {label}
          </NavLink>
        ))}
      </div>

      <Outlet />
    </div>
  );
}
