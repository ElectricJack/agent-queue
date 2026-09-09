import type { PoolProjectStatus } from "../../api/hooks";
import { projectLiveCount, projectQuarantineSeconds } from "./pools";

/**
 * Where one pool's workers actually are.
 *
 * Sizing is fleet-wide, but placement is not: a worker is launched against one
 * project's worktree and stays there for its life, and the placer deliberately
 * concentrates warmth in the busiest project (global worker pools §7.2). So a
 * single supply line — "idle 1, busy 2" — no longer answers the question an
 * operator actually has, which is *which project* those three workers are in
 * and why a fourth is not starting in theirs. That is this table: one row per
 * project the pool is eligible for, with the two placement inputs (the
 * project's own concurrency cap and its free workspace slots) beside the
 * supply, and the quarantine that is keeping the placer out.
 */
export default function PoolProjects({ projects }: { projects: PoolProjectStatus[] }) {
  if (projects.length === 0) {
    return (
      <p className="text-xs text-gray-500">
        No project is eligible for this pool yet — a pool places workers only into active projects
        with a free workspace slot.
      </p>
    );
  }
  return (
    <section aria-label="Workers by project" className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400">Workers by project</h3>
      <div className="overflow-x-auto rounded border border-gray-800">
        <table className="w-full min-w-[34rem] text-left text-[11px]">
          <thead className="bg-gray-900/60 text-[10px] uppercase tracking-wide text-gray-500">
            <tr>
              <th scope="col" className="px-2 py-1 font-medium">Project</th>
              <th scope="col" className="px-2 py-1 text-right font-medium">Ready</th>
              <th scope="col" className="px-2 py-1 text-right font-medium">Idle</th>
              <th scope="col" className="px-2 py-1 text-right font-medium">Busy</th>
              <th scope="col" className="px-2 py-1 text-right font-medium">Starting</th>
              <th scope="col" className="px-2 py-1 text-right font-medium">Draining</th>
              <th scope="col" className="px-2 py-1 text-right font-medium" title="Live workers against the project's own max_concurrent_agents cap">Cap</th>
              <th scope="col" className="px-2 py-1 text-right font-medium" title="Workspace slots the placer can still acquire in this project">Slots</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/70 font-mono text-gray-300">
            {projects.map((project) => (
              <tr key={project.project_id}
                className={projectQuarantineSeconds(project) > 0 ? "bg-amber-500/5" : undefined}>
                <th scope="row" className="max-w-[14rem] truncate px-2 py-1 text-left font-sans font-normal text-gray-200"
                  title={project.project_id}>
                  {project.project_id}
                </th>
                <td className={"px-2 py-1 text-right " + ((project.ready ?? 0) > 0 ? "text-emerald-400" : "")}>{project.ready ?? 0}</td>
                <td className="px-2 py-1 text-right">{project.running_idle ?? 0}</td>
                <td className={"px-2 py-1 text-right " + ((project.running_busy ?? 0) > 0 ? "text-emerald-400" : "")}>{project.running_busy ?? 0}</td>
                <td className="px-2 py-1 text-right">{project.starting ?? 0}</td>
                <td className="px-2 py-1 text-right">{project.draining ?? 0}</td>
                <td className="px-2 py-1 text-right text-gray-500">
                  {projectLiveCount(project)}/{project.max_concurrent_agents ?? "∞"}
                </td>
                <td className="px-2 py-1 text-right text-gray-500">{project.workspace_capacity ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {projects.map((project) => (
        <ProjectQuarantineDetail key={project.project_id} project={project} />
      ))}
    </section>
  );
}

/**
 * A quarantined project, with the whole captured reason.
 *
 * The reason is the harness's own startup output, which is where the actual
 * cause lives — a missing binary, an auth prompt, a refused worktree. Trimming
 * it to a line makes the field useless, which is the complaint that shaped the
 * CLI's own rendering, so it is shown in full: wrapped, monospaced, and given
 * its own scroll box so a hundred lines of output cannot push the table off
 * the screen.
 */
function ProjectQuarantineDetail({ project }: { project: PoolProjectStatus }) {
  const seconds = projectQuarantineSeconds(project);
  if (!seconds) return null;
  return (
    <div className="space-y-1 rounded border border-amber-900/60 bg-amber-500/5 p-2">
      <p className="text-[11px] text-amber-300">
        <span className="font-medium">{project.project_id}</span> quarantined for {Math.ceil(seconds)}s
        <span className="text-amber-300/70"> — a launch failed; the daemon backs off before placing another worker here.</span>
      </p>
      {project.quarantined_reason && (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-950 p-2 font-mono text-[10px] leading-relaxed text-gray-300">
          {project.quarantined_reason}
        </pre>
      )}
    </div>
  );
}
