import { useState } from "react";
import { useApprovePullRequest, usePendingPullRequests } from "../../api/pullRequests";

function openFor(openedAt: number | null): string {
  if (openedAt == null) return "—";
  const hours = Math.max(0, Math.floor((Date.now() / 1000 - openedAt) / 3600));
  if (hours < 1) return "Less than an hour";
  const days = Math.floor(hours / 24);
  if (days > 0) return `${days}d ${hours % 24}h`;
  return `${hours}h`;
}

export default function PullRequestsSection() {
  const pullRequests = usePendingPullRequests();
  const approve = useApprovePullRequest();
  const [approvingTask, setApprovingTask] = useState<string | null>(null);
  const items = pullRequests.data?.pull_requests ?? [];
  const snapshotAge = pullRequests.data?.snapshot_age_seconds;

  const approveRow = async (taskId: string, headSha: string) => {
    setApprovingTask(taskId);
    try {
      await approve.mutateAsync({ taskId, headSha });
    } catch {
      // The mutation exposes the error above.
    } finally {
      setApprovingTask(null);
    }
  };

  return (
    <section aria-labelledby="pull-requests-heading" className="space-y-3">
      <h2 id="pull-requests-heading" className="text-base font-semibold text-gray-100">Pull requests</h2>
      {snapshotAge != null && (
        <p className="text-xs text-gray-500">GitHub snapshot {Math.floor(snapshotAge)}s old</p>
      )}
      {pullRequests.isLoading && <p className="text-sm text-gray-500">Loading pull requests…</p>}
      {pullRequests.error && <p role="alert" className="text-sm text-red-300">Could not load pull requests.</p>}
      {approve.error && <p role="alert" className="text-sm text-red-300">{approve.error.message}</p>}
      {!pullRequests.isLoading && !pullRequests.error && items.length === 0 && (
        <p className="rounded-lg border border-dashed border-gray-800 p-5 text-sm text-gray-500">
          No pending pull requests.
        </p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-800">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="bg-gray-900 text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">PR title</th><th className="px-3 py-2">Repository</th>
                <th className="px-3 py-2">Project</th><th className="px-3 py-2">Task / epic</th>
                <th className="px-3 py-2">Open for</th><th className="px-3 py-2">Review</th>
                <th className="px-3 py-2">CI</th><th className="px-3 py-2">Train</th>
                {pullRequests.data?.can_approve && <th className="px-3 py-2">Action</th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {items.map((pr) => (
                <tr key={`${pr.task_id}:${pr.url}`} className="hover:bg-gray-900/60">
                  <td className="px-3 py-2.5">
                    <a href={pr.url} target="_blank" rel="noopener noreferrer" className="font-medium text-indigo-300 hover:underline">
                      {pr.title}
                    </a>
                  </td>
                  <td className="px-3 py-2.5 text-gray-300">{pr.repository}</td>
                  <td className="px-3 py-2.5 text-gray-300">{pr.project_name}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-400">{pr.task_id}</td>
                  <td className="px-3 py-2.5 text-gray-400">{openFor(pr.opened_at ?? null)}</td>
                  <td className="px-3 py-2.5"><span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-xs text-indigo-200">{pr.review_decision ?? "pending"}</span></td>
                  <td className="px-3 py-2.5"><span className="rounded bg-gray-700/60 px-1.5 py-0.5 text-xs text-gray-200">{pr.ci_status ?? "pending"}</span></td>
                  <td className="px-3 py-2.5 text-gray-400">{pr.train_state ?? "—"}</td>
                  {pullRequests.data?.can_approve && <td className="px-3 py-2.5">
                    {pr.review_decision !== "approved" && <button
                      type="button"
                      disabled={approve.isPending || !pr.head_sha}
                      onClick={() => void approveRow(pr.task_id, pr.head_sha)}
                      className="rounded bg-emerald-700 px-2 py-1 text-xs text-white disabled:opacity-50"
                    >{approvingTask === pr.task_id ? "Approving…" : "Approve"}</button>}
                  </td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
