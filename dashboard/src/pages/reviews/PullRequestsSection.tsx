import { usePendingPullRequests } from "../../api/pullRequests";

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
  const items = pullRequests.data?.pull_requests ?? [];

  return (
    <section aria-labelledby="pull-requests-heading" className="space-y-3">
      <h2 id="pull-requests-heading" className="text-base font-semibold text-gray-100">Pull requests</h2>
      {pullRequests.isLoading && <p className="text-sm text-gray-500">Loading pull requests…</p>}
      {pullRequests.error && <p role="alert" className="text-sm text-red-300">Could not load pull requests.</p>}
      {!pullRequests.isLoading && !pullRequests.error && items.length === 0 && (
        <p className="rounded-lg border border-dashed border-gray-800 p-5 text-sm text-gray-500">
          No pending pull requests.
        </p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-800">
          <table className="w-full min-w-[700px] text-left text-sm">
            <thead className="bg-gray-900 text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">PR title</th><th className="px-3 py-2">Repository</th>
                <th className="px-3 py-2">Project</th><th className="px-3 py-2">Task / epic</th>
                <th className="px-3 py-2">Open for</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {items.map((pr) => (
                <tr key={`${pr.task_id}:${pr.url}`} className="hover:bg-gray-900/60">
                  <td className="px-3 py-2.5">
                    <a href={pr.url} target="_blank" rel="noopener noreferrer" className="font-medium text-indigo-300 hover:underline">
                      {pr.title}
                    </a>
                    {pr.state === "unknown" && (
                      <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-200">state unknown</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-gray-300">{pr.repository}</td>
                  <td className="px-3 py-2.5 text-gray-300">{pr.project_name}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-400">{pr.task_id}</td>
                  <td className="px-3 py-2.5 text-gray-400">{openFor(pr.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
