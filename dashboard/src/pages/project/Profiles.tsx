import { useParams } from "react-router-dom";
import { useProfiles, useProject } from "../../api/hooks";

export default function ProjectProfiles() {
  const { projectId = "" } = useParams();
  const { data: profiles, isLoading } = useProfiles();
  const { data: project } = useProject(projectId);
  const defaultProfileId = project?.default_profile_id ?? null;

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (!profiles?.length) return <p className="text-sm text-gray-500">No profiles defined.</p>;

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        Profiles are system-wide. Each project picks a default; tasks may override.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        {profiles.map((p) => {
          const isDefault = p.id === defaultProfileId;
          return (
            <div
              key={p.id}
              className={`rounded-lg border bg-gray-900 p-4 ${
                isDefault ? "border-indigo-500/50" : "border-gray-800"
              }`}
            >
              <div className="mb-1 flex items-center justify-between">
                <span className="font-medium">{p.name}</span>
                {isDefault && (
                  <span className="rounded-full bg-indigo-500/10 px-2 py-0.5 text-xs font-medium text-indigo-400">
                    project default
                  </span>
                )}
              </div>
              {p.description && (
                <p className="mb-2 text-xs text-gray-400">{p.description}</p>
              )}
              <div className="flex flex-wrap gap-1 text-xs">
                <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">{p.model}</span>
                {p.has_system_prompt && (
                  <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-400">
                    custom prompt
                  </span>
                )}
                {p.mcp_servers.length > 0 && (
                  <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-400">
                    {p.mcp_servers.length} MCP
                  </span>
                )}
                <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-500">
                  {p.allowed_tools.length} tools
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
