import { useEffect, useId, useState } from "react";
import { XMarkIcon, CommandLineIcon, Cog6ToothIcon } from "@heroicons/react/24/outline";
import { PoolInstanceTerminal } from "./AgentTerminal";
import { PoolBadge, PoolQuarantine, PoolSupplyRow } from "./PoolMetadata";
import PoolScaleFields from "./PoolScaleFields";
import { formatIdle, type PoolEntry } from "./pools";

function instanceLabel(instance: PoolEntry["instances"][number]) {
  return [
    instance.name,
    instance.task_id || "unclaimed",
    formatIdle(instance.idle_seconds),
  ].join(" · ");
}

/**
 * One worker pool: its bounds, its live supply, and whichever instance the
 * user has selected. Unlike a fixed worker a pool has no single session — the
 * terminal and the instance metadata below the header follow the selection.
 */
export default function PoolWindow({ entry, instanceId, onInstanceChange, onClose, resetToken }: {
  entry: PoolEntry;
  instanceId: string | null;
  onInstanceChange: (instanceId: string | null) => void;
  onClose: () => void;
  resetToken: string | null;
}) {
  const [tab, setTab] = useState<"terminal" | "settings">("terminal");
  const id = useId();
  useEffect(() => {
    if (resetToken) setTab("terminal");
  }, [resetToken]);

  const { pool, instances } = entry;
  // A pinned instance can drain away between polls; fall back to the pool's
  // oldest live session rather than blanking the view.
  const instance = instances.find((row) => row.id === instanceId) ?? instances[0] ?? null;
  const title = pool.profile_id + " pool";

  const tabs = [
    { id: "terminal" as const, label: "Terminal", Icon: CommandLineIcon },
    { id: "settings" as const, label: "Settings", Icon: Cog6ToothIcon },
  ];

  return (
    <section aria-label={title + " agent window"}
      className="flex min-h-80 min-w-0 flex-col overflow-hidden rounded-xl border border-gray-800 bg-gray-900/40 lg:min-h-0">
      <header className="shrink-0 border-b border-gray-800 bg-gray-900 px-3 py-2">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-sm font-semibold text-gray-100">{title}</h2>
              <PoolBadge />
              <span className="truncate text-[10px] text-gray-500">{pool.project_id}</span>
            </div>
            <p className="mt-0.5"><PoolSupplyRow pool={pool} /></p>
            <PoolQuarantine pool={pool} />
            {instances.length > 0 ? (
              <label className="mt-1 flex min-w-0 items-center gap-2 text-[10px] text-gray-500" htmlFor={id + "-instance"}>
                Instance
                <select id={id + "-instance"} value={instance?.id ?? ""}
                  onChange={(event) => onInstanceChange(event.target.value || null)}
                  className="min-w-0 flex-1 truncate rounded border border-gray-700 bg-gray-950 px-2 py-1 text-xs text-gray-200">
                  {instances.map((row) => (
                    <option key={row.id} value={row.id}>{instanceLabel(row)}</option>
                  ))}
                </select>
              </label>
            ) : (
              <p className="mt-1 text-[10px] text-gray-500">No live instances.</p>
            )}
            {instance && (
              <p className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-gray-500">
                <span className="min-w-0 truncate text-xs text-gray-400"
                  title={(instance.harness || "Harness unknown") + " · " + (instance.model || "Model unknown")}>
                  {instance.harness || "Harness unknown"} · {instance.model || "Model unknown"}
                </span>
                <span>Intelligence: {instance.intelligence_class || "Unknown"}</span>
                <span>State: {instance.state || "unknown"}</span>
                {instance.stalled && <span className="text-amber-300">Stalled</span>}
                <span className="min-w-0 truncate" title={instance.work_dir || "Workspace unknown"}>
                  {instance.work_dir || "Workspace unknown"}
                </span>
              </p>
            )}
            <p className="mt-0.5 truncate text-xs text-gray-400" title={instance?.task_id || ""}>
              {instance ? (instance.task_id || "Idle — waiting to claim work") : "This pool has no running worker."}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <div role="tablist" aria-label={title + " view"} className="flex gap-3">
              {tabs.map(({ id: key, label, Icon }) => (
                <button key={key} type="button" role="tab" id={id + "-" + key}
                  aria-controls={id + "-panel"} aria-selected={tab === key}
                  onClick={() => setTab(key)}
                  className={"flex items-center gap-1.5 rounded border px-2 py-1 text-xs "
                    + (tab === key ? "border-indigo-400/60 bg-indigo-500/10 text-indigo-200" : "border-transparent text-gray-500 hover:text-gray-200")}>
                  <Icon className="h-3.5 w-3.5" />{label}
                </button>
              ))}
            </div>
            <button type="button" aria-label={"Close " + title + " view"} title="Close view (the pool keeps running)" onClick={onClose}
              className="shrink-0 rounded p-1 text-gray-500 hover:bg-gray-800 hover:text-gray-100">
              <XMarkIcon className="h-4 w-4" />
            </button>
          </div>
        </div>
      </header>
      <div role="tabpanel" id={id + "-panel"} aria-labelledby={id + "-" + tab} className="min-h-0 flex-1 overflow-hidden">
        {tab === "terminal" ? <PoolInstanceTerminal instance={instance} /> : (
          <div aria-label={title + " settings"} className="h-full space-y-4 overflow-auto p-4">
            <p className="text-xs leading-relaxed text-gray-400">
              Lifecycle: <span className="text-gray-200">pool</span>. The daemon sizes this pool
              between the bounds below; individual instances are started and drained
              automatically and cannot be added or deleted by hand.
            </p>
            <PoolScaleFields pool={pool} />
          </div>
        )}
      </div>
    </section>
  );
}
