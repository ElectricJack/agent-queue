import { Link } from "react-router-dom";
import { useWaitExplain } from "../api/waits";
import { VerifiedWaitEvidence, WaitDetails } from "./WaitEvidence";

export default function TaskWaitPanel({ taskId, projectId }: { taskId: string; projectId: string }) {
  const { data, isPending, error } = useWaitExplain(taskId, projectId);
  const waits = data?.explanation ? [data.explanation] : data?.waits ?? [];
  return <section aria-label="Wait explanation" className="space-y-2 rounded-lg border border-gray-800 bg-gray-900 p-3">
    <div className="flex items-center justify-between gap-3">
      <h3 className="text-xs font-semibold uppercase text-gray-500">Waiting on</h3>
      <Link to={`/waiting?project=${encodeURIComponent(projectId)}`} className="text-xs text-indigo-400 hover:underline">Project wait graph</Link>
    </div>
    {isPending && <p role="status" className="text-xs text-gray-400">Loading wait evidence…</p>}
    {error && <p role="alert" className="text-xs text-amber-300">Wait explanation unavailable: {error.message}</p>}
    {data?.task && <p className="text-xs text-gray-400">{data.task.lifecycle} · base priority {data.task.base_priority} · effective priority {data.task.effective_priority}
      {data.task.donor_task_id && ` · donor ${data.task.donor_task_id} via ${data.task.donor_path?.join(" → ")}`}</p>}
    {data && waits.length === 0 && <p className="text-xs text-gray-400">No active wait records.</p>}
    {waits.map((wait, i) => <article key={wait.wait_id ?? i} className="space-y-2 border-t border-gray-800 pt-2">
      <h4 className="text-xs font-semibold text-gray-200">Verified evidence</h4>
      <VerifiedWaitEvidence wait={wait} />
      <h4 className="text-xs font-semibold text-gray-200">Claimed commentary</h4>
      <p className="whitespace-pre-wrap break-words text-xs text-gray-400">{wait.claimed ?? "None"}</p>
      <WaitDetails wait={wait} />
    </article>)}
    {data?.truncated && <p role="status" className="text-xs text-amber-300">Waits truncated: showing {waits.length} of {data.total}.</p>}
  </section>;
}
