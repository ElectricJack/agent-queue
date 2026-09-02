import type { PoolStatusRow } from "../../api/hooks";
import { poolQuarantineSeconds, poolSupply } from "./pools";

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

export function PoolQuarantine({ pool }: { pool: PoolStatusRow }) {
  const seconds = poolQuarantineSeconds(pool);
  if (!seconds) return null;
  return (
    <span
      className="block text-[10px] text-amber-300"
      title="A launch failed; the daemon is backing off before starting another session for this pool."
    >
      Quarantined for {Math.ceil(seconds)}s
    </span>
  );
}
