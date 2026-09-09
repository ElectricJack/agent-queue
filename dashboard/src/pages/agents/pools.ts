import { useEffect, useMemo, useRef, useState } from "react";
import { useProfiles, usePoolSessions, usePoolStatus, type Profile, type PoolProjectStatus, type PoolStatusRow, type SessionSummary } from "../../api/hooks";
import type { FlockAgent } from "../../api/agents";

/** One worker pool — a profile, fleet-wide — with the sessions running it. */
export interface PoolEntry {
  /** Selection key — see ``poolSelectionKey`` in useAgentSelection. */
  key: string;
  profileId: string;
  pool: PoolStatusRow;
  /**
   * Where the pool's supply actually sits, one row per project the daemon
   * placed a worker in (or could place one in). Sizing is fleet-wide but a
   * worker still lives inside the project whose worktree it was launched
   * against, so this is the only answer to "where are my workers running".
   */
  projects: PoolProjectStatus[];
  instances: SessionSummary[];
}

export const POOL_PREFIX = "pool:";

export function poolAddress(profileId: string) {
  return POOL_PREFIX + profileId;
}

/**
 * Join ``pool_status`` rows to their live sessions.
 *
 * A pool is identified by its profile alone: bounds, demand and supply are
 * aggregated across every project, so the join is a profile match and the
 * per-project detail rides along in ``projects``. Instances are ordered oldest
 * first: a pool churns, and a stable order keeps the selected instance from
 * jumping under the user between polls. Projects are ordered by id for the
 * same reason — the busiest project changes between polls, the id does not.
 */
export function poolEntries(pools: PoolStatusRow[], sessions: SessionSummary[]): PoolEntry[] {
  const byProfile = new Map<string, SessionSummary[]>();
  for (const session of sessions) {
    if (!session.profile_id) continue;
    byProfile.set(session.profile_id, [...(byProfile.get(session.profile_id) ?? []), session]);
  }
  return [...pools]
    .sort((a, b) => a.profile_id.localeCompare(b.profile_id))
    .map((pool) => ({
      key: poolAddress(pool.profile_id),
      profileId: pool.profile_id,
      pool,
      projects: [...(pool.projects ?? [])].sort((a, b) => a.project_id.localeCompare(b.project_id)),
      instances: [...(byProfile.get(pool.profile_id) ?? [])].sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0)),
    }));
}

export interface BusyPoolEntries {
  busy: PoolEntry[];
  hiddenCount: number;
}

/** Pools with a task-holding worker are the only pools shown in the flock rail. */
export function splitBusyPoolEntries(entries: PoolEntry[]): BusyPoolEntries {
  const busy = entries.filter((entry) => entry.pool.running_busy > 0);
  return { busy, hiddenCount: entries.length - busy.length };
}

/** What the rail actually shows for a split — the debounce compares this, not array identity. */
function busySignature(split: BusyPoolEntries): string {
  return split.busy.map((entry) => entry.key + "=" + entry.pool.running_busy).join("|") + "#" + split.hiddenCount;
}

/**
 * Hold a pool's rail visibility briefly when supply changes so a claim or
 * completion does not make the flock jump between adjacent live updates.
 *
 * The hold is keyed on *what would be shown*, never on the identity of the
 * ``entries`` array: ``usePoolFlock`` hands the rail a fresh array on every
 * render, and every agent/session/task/message event re-renders the rail, so
 * a timer that re-armed per render never fired while the fleet was busy and a
 * pool that had just claimed work stayed hidden for as long as it worked.
 * Now a render whose target matches the pending one leaves the timer alone,
 * a render that reverts to the visible state cancels it, and only a genuinely
 * new target restarts the hold.
 */
export function useDebouncedBusyPoolEntries(entries: PoolEntry[], delay = 1_000): BusyPoolEntries {
  const next = useMemo(() => splitBusyPoolEntries(entries), [entries]);
  const [visible, setVisible] = useState(next);
  const initialized = useRef(false);
  const hasReceivedEntries = useRef(entries.length > 0);
  const visibleSignature = useRef(busySignature(next));
  const pending = useRef<{ signature: string; timer: number } | null>(null);

  useEffect(() => {
    const target = busySignature(next);
    const commit = () => {
      pending.current = null;
      visibleSignature.current = target;
      setVisible(next);
    };
    if (!initialized.current) {
      initialized.current = true;
      commit();
      return;
    }
    // The first status response should populate the rail immediately. Later
    // changes are debounced to avoid a claim/completion flicker.
    if (!hasReceivedEntries.current && entries.length > 0) {
      hasReceivedEntries.current = true;
      commit();
      return;
    }
    if (target === visibleSignature.current) {
      // Back to (or still at) what is shown: nothing to hold for.
      if (pending.current) {
        window.clearTimeout(pending.current.timer);
        pending.current = null;
      }
      return;
    }
    if (pending.current?.signature === target) return; // same target: let the hold run
    if (pending.current) window.clearTimeout(pending.current.timer);
    pending.current = { signature: target, timer: window.setTimeout(commit, delay) };
  }, [entries, delay, next]);

  useEffect(() => () => { if (pending.current) window.clearTimeout(pending.current.timer); }, []);

  return visible;
}

/**
 * Profile IDs that run as pools.
 *
 * ``list_profiles`` reports each profile's own ``lifecycle`` and is the
 * authoritative answer: it covers a pool profile that no active project has
 * measured yet, which is exactly the case the create-a-pool form has to
 * offer. ``pool_status`` rows are unioned in so a pool the daemon is already
 * sizing is never treated as ineligible because of a stale profile list.
 */
export function poolProfileIds(pools: PoolStatusRow[], profiles: Profile[] = []): Set<string> {
  return new Set([
    ...pools.map((pool) => pool.profile_id),
    ...profiles.filter(isPoolProfile).map((profile) => profile.id),
  ]);
}

/** A profile that defines elastic pool capacity rather than one durable worker. */
export function isPoolProfile(profile: Profile): boolean {
  return profile.lifecycle === "pool";
}

/**
 * A worker whose profile runs as a pool is a pool instance, not a fixed push
 * agent: `_launch_pool_session` mints (or reuses) an agent row per session, and
 * the push scheduler never routes work to a pool profile. Those rows are
 * reachable through their pool entry, so the flock lists them there instead.
 */
export function isPoolAgent(agent: FlockAgent, poolIds: Set<string>): boolean {
  return poolIds.has(agent.profile_id);
}

/**
 * How long a *project* is still quarantined for this pool.
 *
 * A quarantine is a launch failure in one project's workspace, never a
 * property of the global pool, so it is read per project row and the pool is
 * described as quarantined only where it actually cannot start a worker.
 */
export function projectQuarantineSeconds(project: PoolProjectStatus, now = Date.now() / 1000): number {
  const until = project.quarantined_until;
  return until && until > now ? until - now : 0;
}

/** The projects a pool currently cannot start a worker in, soonest first. */
export function quarantinedProjects(projects: PoolProjectStatus[], now = Date.now() / 1000): PoolProjectStatus[] {
  return projects
    .filter((project) => projectQuarantineSeconds(project, now) > 0)
    .sort((a, b) => projectQuarantineSeconds(a, now) - projectQuarantineSeconds(b, now));
}

/** Live workers (idle + busy) a project is holding for this pool. */
export function projectLiveCount(project: PoolProjectStatus): number {
  return (project.running_idle ?? 0) + (project.running_busy ?? 0);
}

/**
 * "where the workers are", short enough for a rail row.
 *
 * Warmth deliberately concentrates in the busiest project (global worker pools
 * §7.2), so the placement an operator needs at a glance is the ordering by
 * live workers, not the full table — that lives in the pool's detail view.
 * Projects holding nothing are dropped: a pool row would otherwise list every
 * active project on the box.
 */
export function poolPlacement(projects: PoolProjectStatus[]): PoolProjectStatus[] {
  return projects
    .filter((project) => projectLiveCount(project) > 0 || (project.starting ?? 0) > 0)
    .sort((a, b) => projectLiveCount(b) - projectLiveCount(a) || a.project_id.localeCompare(b.project_id));
}

/** The supply breakdown, in the order the CLI's `aq pool status` prints it. */
export function poolSupply(pool: PoolStatusRow): { key: string; label: string; value: number }[] {
  return [
    { key: "desired", label: "desired", value: pool.desired },
    { key: "idle", label: "idle", value: pool.running_idle },
    { key: "busy", label: "busy", value: pool.running_busy },
    { key: "starting", label: "starting", value: pool.starting },
    { key: "draining", label: "draining", value: pool.draining },
    { key: "ready", label: "ready", value: pool.ready },
  ];
}

export function formatIdle(seconds: number | undefined | null): string {
  const value = Math.max(0, Math.round(seconds ?? 0));
  if (value < 60) return value + "s idle";
  if (value < 3600) return Math.floor(value / 60) + "m idle";
  return Math.floor(value / 3600) + "h idle";
}

/** Pools plus their instances, on one refresh cadence, for the rail and the workspace. */
export function usePoolFlock() {
  const pools = usePoolStatus();
  const sessions = usePoolSessions();
  const profiles = useProfiles();
  // React Query keeps ``data`` referentially stable across equal refetches, so
  // memoising here means an unrelated re-render of the rail does not hand its
  // consumers a new array to react to.
  const entries = useMemo(() => poolEntries(pools.data ?? [], sessions.data ?? []), [pools.data, sessions.data]);
  const poolIds = useMemo(() => poolProfileIds(pools.data ?? [], profiles.data ?? []), [pools.data, profiles.data]);
  return { entries, poolIds, isLoading: pools.isLoading, error: pools.error };
}
