import { usePullRequestProjectSummaries } from "../../api/pullRequests";

export default function PullRequestsSection() {
  const pullRequests = usePullRequestProjectSummaries();
  const projects = pullRequests.data?.projects ?? [];
  const snapshotAge = pullRequests.data?.snapshot_age_seconds;
  const hasSnapshot = pullRequests.data?.snapshot_at != null;

  return (
    <section aria-labelledby="pull-requests-heading" className="space-y-3">
      <h2 id="pull-requests-heading" className="text-base font-semibold text-gray-100">
        Pull requests
      </h2>
      <p className="text-xs text-gray-500">Task-linked open pull requests by project.</p>
      {snapshotAge != null && (
        <p className="text-xs text-gray-500">GitHub snapshot {Math.floor(snapshotAge)}s old</p>
      )}
      {pullRequests.isLoading && <p className="text-sm text-gray-500">Loading pull requests…</p>}
      {pullRequests.error && (
        <p role="alert" className="text-sm text-red-300">Could not load pull requests.</p>
      )}
      {!pullRequests.isLoading && !pullRequests.error && projects.length === 0 && (
        <p className="rounded-lg border border-dashed border-gray-800 p-5 text-sm text-gray-500">
          No active projects with GitHub repositories.
        </p>
      )}
      {projects.length > 0 && (
        <>
          {!hasSnapshot && <p className="text-xs text-gray-500">Waiting for GitHub snapshot…</p>}
          <div className="overflow-x-auto rounded-lg border border-gray-800">
            <table aria-label="Project pull requests" className="w-full text-left text-sm">
              <thead className="bg-gray-900 text-xs uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-3 py-2">Project</th>
                  <th className="px-3 py-2">Open PRs</th>
                  <th className="px-3 py-2">Repository</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {projects.map((project) => (
                  <tr key={project.project_id} className="hover:bg-gray-900/60">
                    <td className="px-3 py-2.5 font-medium text-gray-300">
                      {project.project_name}
                    </td>
                    <td className="px-3 py-2.5 tabular-nums text-gray-300">
                      {hasSnapshot ? project.open_pr_count : "—"}
                    </td>
                    <td className="px-3 py-2.5">
                      <a
                        href={project.pulls_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label={`View pull requests for ${project.project_name}`}
                        className="text-indigo-300 hover:underline"
                      >
                        {project.repository}
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
