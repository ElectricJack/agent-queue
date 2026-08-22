import { useState } from "react";
import { NavLink, Outlet, useNavigate, useParams } from "react-router-dom";
import {
  ExclamationTriangleIcon,
  PauseIcon,
  PlayIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import Modal from "../../components/Modal";
import {
  useDeleteProject,
  usePauseProject,
  useProject,
  useResumeProject,
} from "../../api/hooks";

const tabs: Array<{ to: string; label: string; end?: boolean }> = [
  { to: ".", label: "Overview", end: true },
  { to: "tasks", label: "Tasks" },
  { to: "sessions", label: "Sessions" },
  { to: "chat", label: "Chat" },
  { to: "workspaces", label: "Workspaces" },
  { to: "profiles", label: "Profiles" },
  { to: "playbooks", label: "Playbooks" },
  { to: "config", label: "Config" },
];

export default function ProjectLayout() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const { data: project, isLoading } = useProject(projectId);
  const pause = usePauseProject();
  const resume = useResumeProject();
  const del = useDeleteProject();
  const paused = !!project?.paused;
  const pending = pause.isPending || resume.isPending;

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const nameForConfirm = project?.name || projectId;
  const canDelete = confirmText.trim() === nameForConfirm;

  const doDelete = async () => {
    setDeleteError(null);
    try {
      await del.mutateAsync({ project_id: projectId });
      setConfirmOpen(false);
      navigate("/");
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Project</p>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold">
            {isLoading ? projectId : project?.name || projectId}
          </h1>
          {project && (
            <>
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
              <button
                type="button"
                onClick={() => {
                  setConfirmText("");
                  setDeleteError(null);
                  setConfirmOpen(true);
                }}
                className="inline-flex h-8 w-8 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-red-500/10 hover:text-red-400"
                title="Delete project"
                aria-label="Delete project"
              >
                <TrashIcon className="h-4 w-4" />
              </button>
            </>
          )}
        </div>
        {project?.repo_url && (
          <p className="font-mono text-xs text-gray-500">{project.repo_url}</p>
        )}
      </header>

      <div className="flex gap-1 overflow-x-auto border-b border-gray-800">
        {tabs.map(({ to, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `whitespace-nowrap px-4 py-2 text-sm font-medium transition-colors ${
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

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="Delete project"
      >
        <div className="space-y-4 text-sm">
          <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 p-3 text-red-300">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <p className="font-semibold">This is irreversible.</p>
              <p className="mt-1 text-red-300/80">
                Deletes the project row, its tasks, workspaces, gates, and
                related state. Files on disk (repo checkout, worktrees, vault
                notes) are NOT touched — remove them manually if needed.
              </p>
            </div>
          </div>
          <p className="text-gray-300">
            Type the project name{" "}
            <span className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-xs text-gray-100">
              {nameForConfirm}
            </span>{" "}
            to confirm.
          </p>
          <input
            type="text"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder={nameForConfirm}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-red-500 focus:outline-none"
            autoFocus
          />
          {deleteError && (
            <p className="rounded border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-300">
              {deleteError}
            </p>
          )}
          <div className="flex justify-end gap-2 border-t border-gray-800 pt-3">
            <button
              type="button"
              onClick={() => setConfirmOpen(false)}
              className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={doDelete}
              disabled={!canDelete || del.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-gray-700"
            >
              <TrashIcon className="h-4 w-4" />
              {del.isPending ? "Deleting…" : "Delete project"}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
