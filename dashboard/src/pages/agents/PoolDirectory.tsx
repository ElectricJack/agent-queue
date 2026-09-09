import { UsersIcon } from "@heroicons/react/24/outline";
import { usePoolSetEnabled } from "../../api/hooks";
import EnableToggle from "./EnableToggle";
import { PoolBadge, PoolPlacementRow, PoolQuarantine, PoolSupplyRow } from "./PoolMetadata";
import type { PoolEntry } from "./pools";

/**
 * Every configured pool, busy or idle, as an openable list.
 *
 * The flock rail only lists pools with a task-holding worker; its "N idle
 * pools" link lands here, so this is the one place the whole set is visible —
 * and therefore where an idle pool is switched off (a fable-tier pool low on
 * remaining usage, say) and back on again.
 */
export default function PoolDirectory({ entries, onOpen }: { entries: PoolEntry[]; onOpen: (key: string) => void }) {
  // One mutation for the list: only one row can be in flight at a time, and
  // ``variables`` says which, so a refusal is reported on the row that caused it.
  const setEnabled = usePoolSetEnabled();
  if (entries.length === 0) return null;
  return (
    <section aria-label="Worker pools" className="shrink-0 rounded-xl border border-gray-800">
      <h2 className="border-b border-gray-800 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-400">
        Worker pools
        <span className="ml-2 font-mono text-[10px] normal-case text-gray-500">{entries.length}</span>
      </h2>
      <ul className="divide-y divide-gray-800/70">
        {entries.map((entry) => {
          const live = entry.pool.running_idle + entry.pool.running_busy;
          const enabled = entry.pool.enabled !== false;
          const target = setEnabled.variables?.profile_id === entry.profileId;
          return (
            <li key={entry.key} className="flex items-start gap-2 px-3 py-2 hover:bg-gray-800/60">
              <button
                type="button"
                aria-label={"Open pool " + entry.profileId}
                onClick={() => onOpen(entry.key)}
                className="flex min-w-0 flex-1 flex-col gap-1 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400"
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-gray-200">{entry.profileId}</span>
                  <span className="flex shrink-0 items-center gap-2">
                    <span className={"text-[10px] " + (entry.pool.running_busy > 0 ? "text-emerald-400" : "text-gray-500")}>
                      {!enabled ? "disabled" : entry.pool.running_busy > 0 ? "busy" : live > 0 ? "idle" : "no workers"}
                    </span>
                    <PoolBadge />
                  </span>
                </span>
                <PoolPlacementRow projects={entry.projects} />
                <PoolSupplyRow pool={entry.pool} />
                <PoolQuarantine projects={entry.projects} />
                {!enabled && (
                  <span className="block text-[10px] text-amber-300">
                    Disabled — no new work is claimed; workers already on a task finish it.
                  </span>
                )}
              </button>
              <EnableToggle
                enabled={enabled}
                subject={entry.profileId + " pool"}
                pending={setEnabled.isPending && target}
                error={target && setEnabled.error ? setEnabled.error.message : null}
                onChange={(next) => setEnabled.mutate({ profile_id: entry.profileId, enabled: next })}
              />
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** The empty state shown beside the directory when no agent view is open. */
export function PoolDirectoryHint() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-gray-800 p-8 text-center">
      <UsersIcon className="h-10 w-10 text-gray-700" />
      <p className="text-sm text-gray-400">Select an agent from the flock to open its live terminal and settings.</p>
      <p className="text-xs text-gray-600">Closing a view never stops the agent.</p>
    </div>
  );
}
