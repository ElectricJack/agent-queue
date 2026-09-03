import { useEffect, useMemo, useRef, useState } from "react";
import { usePoolSessions, usePoolStatus, type PoolStatusRow, type SessionSummary } from "../../api/hooks";
import type { FlockAgent } from "../../api/agents";

/** A pool profile in one project, together with the sessions currently running it. */
export interface PoolEntry {
  /** Selection key — see ``poolSelectionKey`` in useAgentSelection. */
  key: string;
  projectId: string;
  profileId: string;
  pool: PoolStatusRow;
  instances: SessionSummary[];
}

export const POOL_PREFIX = "pool:";

export function poolAddress(projectId: string, profileId: string) {
  return POOL_PREFIX + projectId + ":" + profileId;
}

/**
 * Join ``pool_status`` rows to their live sessions.
 *
 * Sessions carry ``project_id``/``profile_id`` in the plain (unscoped) form
 * the pool sizer uses, so the join is a straight key match. Instances are
 * ordered oldest first: a pool churns, and a stable order keeps the selected
 * instance from jumping under the user between polls.
 */
export function poolEntries(pools: PoolStatusRow[], sessions: SessionSummary[]): PoolEntry[] {
  const byKey = new Map<string, SessionSummary[]>();
  for (const session of sessions) {
    if (!session.project_id || !session.profile_id) continue;
    const key = poolAddress(session.project_id, session.profile_id);
    byKey.set(key, [...(byKey.get(key) ?? []), session]);
  }
  return [...pools]
    .sort((a, b) => a.profile_id.localeCompare(b.profile_id) || a.project_id.localeCompare(b.project_id))
    .map((pool) => {
      const key = poolAddress(pool.project_id, pool.profile_id);
      return {
        key,
        projectId: pool.project_id,
        profileId: pool.profile_id,
        pool,
        instances: [...(byKey.get(key) ?? [])].sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0)),
      };
    });
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

/**
 * Hold a pool's rail visibility briefly when supply changes so a claim or
 * completion does not make the flock jump between adjacent live updates.
 */
export function useDebouncedBusyPoolEntries(entries: PoolEntry[], delay = 1_000): BusyPoolEntries {
  const next = useMemo(() => splitBusyPoolEntries(entries), [entries]);
  const [visible, setVisible] = useState(next);
  const initialized = useRef(false);
  const hasReceivedEntries = useRef(entries.length > 0);

  useEffect(() => {
    if (!initialized.current) {
      initialized.current = true;
      setVisible(next);
      return;
    }
    // The first status response should populate the rail immediately. Later
    // changes are debounced to avoid a claim/completion flicker.
    if (!hasReceivedEntries.current && entries.length > 0) {
      hasReceivedEntries.current = true;
      setVisible(next);
      return;
    }
    const timer = window.setTimeout(() => setVisible(next), delay);
    return () => window.clearTimeout(timer);
  }, [entries, delay, next]);

  return visible;
}

/**
 * Profile IDs that run as pools anywhere. `pool_status` is the only source.
 * This says how a *profile* is launched; it says nothing about whether one
 * worker row is a pool instance — see `isPoolAgent`.
 */
export function poolProfileIds(pools: PoolStatusRow[]): Set<string> {
  return new Set(pools.map((pool) => pool.profile_id));
}

/**
 * A pool instance is a row a pool minted (`origin: "pool"`, reused between
 * sessions and idle in between) or any row a `lifecycle: pool` session
 * currently owns — `_launch_pool_session` reserves an idle hand-made worker
 * on a compatible profile just as readily. Either way the row is reachable
 * through its pool entry, so the flock lists it there instead of beside the
 * fixed workers. The profile id is not a signal: `pool_status` names every
 * pool profile in every active project even at zero supply, and a worker
 * someone added on that profile is a plain durable worker until a pool
 * session takes it.
 */
export function isPoolAgent(agent: FlockAgent): boolean {
  return agent.origin === "pool" || agent.session_lifecycle === "pool";
}

export function poolQuarantineSeconds(pool: PoolStatusRow, now = Date.now() / 1000): number {
  const until = pool.quarantined_until;
  return until && until > now ? until - now : 0;
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
  return {
    entries: poolEntries(pools.data ?? [], sessions.data ?? []),
    isLoading: pools.isLoading,
    error: pools.error,
  };
}
