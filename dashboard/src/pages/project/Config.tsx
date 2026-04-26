import { useState } from "react";
import { useParams } from "react-router-dom";
import { TrashIcon } from "@heroicons/react/24/outline";
import { useProject } from "../../api/hooks";
import DeleteProjectModal from "../../components/DeleteProjectModal";

export default function ProjectConfig() {
  const { projectId = "" } = useParams();
  const { data: project, isLoading } = useProject(projectId);
  const [deleteOpen, setDeleteOpen] = useState(false);

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (!project) return <p className="text-sm text-gray-500">Project not found.</p>;

  const rows: Array<[string, React.ReactNode]> = [
    ["ID", <span className="font-mono">{project.id}</span>],
    ["Name", project.name],
    ["Repo path", project.repo_path ? <span className="font-mono text-xs">{project.repo_path}</span> : "—"],
    ["Default branch", project.default_branch ?? "—"],
    ["Default profile", project.default_profile_id ?? "—"],
    ["Discord channel", project.discord_channel_id ?? "—"],
    ["Budget limit", project.budget_limit != null ? String(project.budget_limit) : "—"],
    ["Status", project.paused ? "Paused" : project.is_active === false ? "Inactive" : "Active"],
  ];

  return (
    <div className="space-y-6">
      <p className="text-xs text-gray-500">Editing project config from the dashboard isn't wired yet.</p>
      <dl className="overflow-hidden rounded-lg border border-gray-800">
        {rows.map(([label, value], i) => (
          <div
            key={String(label)}
            className={`grid grid-cols-3 gap-4 px-4 py-3 text-sm ${
              i % 2 === 0 ? "bg-gray-900" : "bg-gray-900/50"
            }`}
          >
            <dt className="text-gray-400">{label}</dt>
            <dd className="col-span-2 text-gray-200">{value}</dd>
          </div>
        ))}
      </dl>

      <section className="rounded-lg border border-red-500/30 bg-red-500/5 p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <h3 className="text-sm font-semibold text-red-300">Danger zone</h3>
            <p className="text-xs text-red-300/70">
              Deleting a project removes its tasks, workspaces, and constraints. This cannot be
              undone.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setDeleteOpen(true)}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-sm font-medium text-red-300 transition-colors hover:bg-red-500/20 hover:text-red-200"
          >
            <TrashIcon className="h-4 w-4" />
            Delete project
          </button>
        </div>
      </section>

      <DeleteProjectModal
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        projectId={project.id}
        projectName={project.name}
      />
    </div>
  );
}
