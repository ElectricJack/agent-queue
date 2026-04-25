import { useParams } from "react-router-dom";
import { useProject } from "../../api/hooks";

export default function ProjectConfig() {
  const { projectId = "" } = useParams();
  const { data: project, isLoading } = useProject(projectId);

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
    <div className="space-y-4">
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
    </div>
  );
}
