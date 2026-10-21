import { Link, useSearchParams } from "react-router-dom";
import { useWaitGraph } from "../api/waits";
import { useProjects } from "../api/hooks";
import { VerifiedWaitEvidence, WaitDetails } from "../components/WaitEvidence";
import StatusBadge from "../components/StatusBadge";

export default function WaitingOn() {
  const [params, setParams] = useSearchParams();
  const projectId = params.get("project") || undefined;
  const { data: projects } = useProjects();
  const { data, isPending, error } = useWaitGraph(projectId);
  return <div className="space-y-4 p-4 md:p-6">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-xl font-semibold text-gray-100">Waiting on</h1>
      <label className="text-sm text-gray-400">Project <select value={projectId ?? ""}
        onChange={(event) => setParams(event.target.value ? { project: event.target.value } : {})}
        className="ml-2 rounded border border-gray-700 bg-gray-900 p-2 text-gray-200">
        <option value="">All projects</option>
        {projectId && !projects?.some((project) => project.id === projectId) && <option value={projectId}>{projectId}</option>}
        {projects?.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
      </select></label>
    </div>
    {isPending && <p role="status">Loading wait graph…</p>}
    {error && <p role="alert" className="text-amber-300">Wait graph unavailable: {error.message}</p>}
    {data && data.projects?.length === 0 && <p>No project waits.</p>}
    {data?.truncated && <p role="status" className="text-amber-300">Project list truncated.</p>}
    {data?.projects?.map((project) => <section key={project.project_id} className="space-y-3 rounded-lg border border-gray-800 p-3">
      <h2 className="text-lg text-gray-200">{project.project_id} · {project.total_waits} pending waits</h2>
      <p className="text-xs text-gray-400">{project.counts?.map((count) => `${count.kind}: ${count.count} (${count.decision ?? "undecided"})`).join(" · ")}</p>
      {project.waits?.length === 0 && <p>No active wait records.</p>}
      <div className="overflow-x-auto"><table className="w-full text-left text-sm">
        <thead className="text-gray-400"><tr><th className="p-2">Owner</th><th className="p-2">Verified evidence</th><th className="p-2">Claimed commentary</th><th className="p-2">Age and admission</th></tr></thead>
        <tbody>{project.waits?.map((wait, i) => <tr key={wait.wait_id ?? i} className="border-t border-gray-800 align-top">
          <td className="p-2 text-xs">
            {wait.owner?.id && (wait.owner.kind === "task" || wait.owner.kind === "scheduler")
              ? <Link to={`/tasks/${encodeURIComponent(wait.owner.id)}`} className="text-indigo-400 hover:underline">{wait.owner.id}</Link>
              : <span>{wait.owner?.kind}:{wait.owner?.id}</span>}
            <div className="mt-1"><StatusBadge status={wait.lifecycle ?? wait.outcome} /></div>
          </td>
          <td className="min-w-64 p-2"><VerifiedWaitEvidence wait={wait} /></td>
          <td className="min-w-40 whitespace-pre-wrap break-words p-2 text-xs text-gray-400">{wait.claimed ?? "None"}</td>
          <td className="min-w-64 p-2"><WaitDetails wait={wait} /></td>
        </tr>)}</tbody>
      </table></div>
      <details><summary className="cursor-pointer text-sm text-gray-300">Chains, escalations, branch blockers and held messages</summary>
        <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words text-xs text-gray-400">{JSON.stringify({
          longest_chains: project.longest_chains, deadlocks: project.deadlocks,
          escalated_waits: project.escalated_waits, branch_blockers: project.branch_blockers,
          held_message_backlog: project.held_message_backlog,
        }, null, 2)}</pre>
      </details>
      {Object.values(project.truncated ?? {}).some(Boolean) && <p role="status" className="text-xs text-amber-300">Project results truncated: {Object.entries(project.truncated ?? {}).filter(([, value]) => value).map(([key]) => key).join(", ")}.</p>}
    </section>)}
  </div>;
}
