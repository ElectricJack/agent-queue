import type { PoolProjectStatus, PoolStatusRow } from "../../api/hooks";
import { poolPlacement, poolSupply, projectLiveCount, projectQuarantineSeconds, quarantinedProjects } from "./pools";

/** Distinguishes a pull-based pool profile from a fixed push worker. */
export function PoolBadge({ className = "" }: { className?: string }) {
  return (
    <span
      title="Worker pool: sessions are started by the daemon and pull tasks with `aq task claim`."
      className={"shrink-0 rounded bg-sky-500/10 px-1.5 py-0.5 text-[10px] text-sky-300 " + className}
    >
      Pool
    </span>
  );
}

/** desired / idle / busy / starting / draining / ready, straight from pool_status. */
export function PoolSupplyRow({ pool }: { pool: PoolStatusRow }) {
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[10px] text-gray-500">
      {poolSupply(pool).map(({ key, label, value }) => (
        <span key={key} className={value > 0 && (key === "busy" || key === "ready") ? "text-emerald-400" : undefined}>
          {label} {value}
        </span>
      ))}
      <span title={"Bounds: min_active " + pool.min_active + ", max_active " + (pool.max_active ?? "unbounded")}>
        [{pool.min_active}–{pool.max_active ?? "∞"}]
      </span>
    </span>
  );
}

/** Task-lifecycle sessions that consume a route this pool could have served. */
export function PoolOutsidePools({ pool }: { pool: PoolStatusRow }) {
  const outside = pool.outside_pools ?? [];
  if (outside.length === 0) return null;
  const summary = outside.map((session) => session.profile_id + " · " + (session.task_id || "no task")).join("\n");
  return (
    <span className="block truncate text-[10px] text-amber-300" title={summary}>
      Outside pools: {outside.length} task-lifecycle session{outside.length === 1 ? "" : "s"} on this route
    </span>
  );
}

/**
 * Which projects a pool's live workers are in, in one line.
 *
 * The supply row above is fleet-wide now, so on its own it hides the thing a
 * pool's bounds do not control: a worker is pinned to the project it was
 * launched into, and the placer concentrates warmth in the busiest one. The
 * rail and the directory have room for exactly this much of that — the full
 * table is in the pool's detail view.
 */
export function PoolPlacementRow({ projects }: { projects: PoolProjectStatus[] }) {
  const placed = poolPlacement(projects);
  if (placed.length === 0) {
    return <span className="block truncate text-[10px] text-gray-500">No workers placed</span>;
  }
  const summary = placed.map((project) => project.project_id + " " + projectLiveCount(project)).join(" · ");
  return <span className="block truncate text-[10px] text-gray-500" title={summary}>{summary}</span>;
}

/**
 * Quarantine, named by project.
 *
 * A quarantine is a launch failure against one project's workspace, so a pool
 * that cannot start a worker in one project may be perfectly healthy in
 * another; saying only "quarantined" would misreport the whole fleet. The
 * captured reason is on the title here and rendered in full in ``PoolProjects``.
 */
export function PoolQuarantine({ projects }: { projects: PoolProjectStatus[] }) {
  const quarantined = quarantinedProjects(projects);
  if (quarantined.length === 0) return null;
  return (
    <span className="block space-y-0.5 text-[10px] text-amber-300">
      {quarantined.map((project) => (
        <span key={project.project_id} className="block truncate"
          title={project.quarantined_reason
            || "A launch failed; the daemon is backing off before starting another session here."}>
          Quarantined in {project.project_id} for {Math.ceil(projectQuarantineSeconds(project))}s
        </span>
      ))}
    </span>
  );
}
