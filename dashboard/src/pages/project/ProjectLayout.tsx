import { PauseIcon, PlayIcon } from "@heroicons/react/24/outline";
import { NavLink, Outlet, useParams } from "react-router-dom";
import { usePauseProject, useProject, useResumeProject } from "../../api/hooks";

const tabs: Array<{ to: string; label: string; end?: boolean }> = [
  { to: ".", label: "Overview", end: true },
  { to: "tasks", label: "Tasks" },
  { to: "workspaces", label: "Workspaces" },
  { to: "profiles", label: "Profiles" },
  { to: "playbooks", label: "Playbooks" },
  { to: "config", label: "Config" },
];

export default function ProjectLayout() {
  const { projectId = "" } = useParams();
  const { data: project, isLoading } = useProject(projectId);
  const pause = usePauseProject();
  const resume = useResumeProject();
  const paused = !!project?.paused;
  const pending = pause.isPending || resume.isPending;

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Project</p>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold">
            {isLoading ? projectId : project?.name || projectId}
          </h1>
          {project && (
            <button
              type="button"
              onClick={() =>
                paused
                  ? resume.mutate({ project_id: projectId })
                  : pause.mutate({ project_id: projectId })
              }
              disabled={pending}
              className={`inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                paused
                  ? "bg-amber-500/10 text-amber-400 hover:bg-amber-500/20"
                  : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
              }`}
              title={paused ? "Resume project" : "Pause project"}
              aria-label={paused ? "Resume project" : "Pause project"}
            >
              {paused ? <PlayIcon className="h-4 w-4" /> : <PauseIcon className="h-4 w-4" />}
            </button>
          )}
        </div>
        {project?.repo_url && (
          <p className="font-mono text-xs text-gray-500">{project.repo_url}</p>
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
