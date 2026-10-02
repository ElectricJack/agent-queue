import { useEffect, useId, useState } from "react";
import { CommandLineIcon, Cog6ToothIcon } from "@heroicons/react/24/outline";
import { PoolInstanceTerminal } from "./AgentTerminal";
import { PoolBadge, PoolOutsidePools, PoolPlacementRow, PoolQuarantine, PoolSupplyRow } from "./PoolMetadata";
import PoolProjects from "./PoolProjects";
import PoolScaleFields from "./PoolScaleFields";
import { formatIdle, type PoolEntry } from "./pools";
import TerminalPane from "../../components/TerminalPane";

function instanceLabel(instance: PoolEntry["instances"][number]) {
  return [
    instance.name,
    // The pool is fleet-wide but each instance is pinned to the project it was
    // launched into, so the picker has to say which one it is about to show.
    instance.project_id || "no project",
    instance.task_id || "unclaimed",
    formatIdle(instance.idle_seconds),
  ].join(" · ");
}

function InstancePicker({ entry, instance, onChange, compact = false }: {
  entry: PoolEntry;
  instance: PoolEntry["instances"][number] | null;
  onChange: (instanceId: string | null) => void;
  compact?: boolean;
}) {
  const id = useId();
  return (
    <label className={compact ? "flex w-32 min-w-11 shrink items-center" : "mt-1 flex min-w-0 items-center gap-2 text-[10px] text-gray-500"} htmlFor={id}>
      <span className={compact ? "sr-only" : undefined}>{compact ? "Terminal for " + entry.pool.profile_id + " pool" : "Instance"}</span>
      <select data-primary-control id={id} value={instance?.id ?? ""}
        title={instance ? instanceLabel(instance) : undefined}
        onChange={(event) => onChange(event.target.value || null)}
        className={"min-w-0 flex-1 truncate rounded border border-gray-700 bg-gray-950 px-2 text-xs text-gray-200 focus-visible:outline-2 focus-visible:outline-indigo-400 " + (compact ? "h-8" : "py-1")}>
        {entry.instances.map((row, index) => (
          <option key={row.id} value={row.id}>{compact ? `${index + 1} · ` : ""}{instanceLabel(row)}</option>
        ))}
      </select>
    </label>
  );
}

/**
 * One worker pool: its bounds, its live supply, and whichever instance the
 * user has selected. Unlike a fixed worker a pool has no single session — the
 * terminal and the instance metadata in the disclosure follow the selection.
 */
export default function PoolWindow({ entry, instanceId, onInstanceChange, onClose, resetToken, focusRequest }: {
  entry: PoolEntry;
  instanceId: string | null;
  onInstanceChange: (instanceId: string | null) => void;
  onClose: () => void;
  resetToken: string | null;
  focusRequest: string | null;
}) {
  const [tab, setTab] = useState<"terminal" | "settings">("terminal");
  const id = useId();
  useEffect(() => {
    if (resetToken || focusRequest) setTab("terminal");
  }, [resetToken, focusRequest]);

  const { pool, projects, instances } = entry;
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
      <TerminalPane title={title} status={instance?.stalled ? "Stalled" : instance?.state || "Idle"} onClose={onClose} titleId={id + "-title"}
        primary={instances.length > 1 ? <InstancePicker entry={entry} instance={instance} onChange={onInstanceChange} compact /> : undefined}
        details={<>
        <PoolBadge />
        <p className="mt-0.5"><PoolSupplyRow pool={pool} /></p>
        <PoolOutsidePools pool={pool} />
        <PoolPlacementRow projects={projects} />
        <PoolQuarantine projects={projects} />
        {instances.length > 0 ? (
          <InstancePicker entry={entry} instance={instance} onChange={onInstanceChange} />
        ) : (
          <p className="mt-1 text-[10px] text-gray-500">No live instances.</p>
        )}
        {instance && (
          <p className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-gray-500">
            <span className="min-w-0 text-xs text-gray-400"
              title={(instance.harness || "Harness unknown") + " · " + (instance.model || "Model unknown")}>
              {instance.harness || "Harness unknown"} · {instance.model || "Model unknown"}
            </span>
            <span>Intelligence: {instance.intelligence_class || "Unknown"}</span>
            <span>State: {instance.state || "unknown"}</span>
            {instance.stalled && <span className="text-amber-300">Stalled</span>}
            <span className="min-w-0" title={instance.work_dir || "Workspace unknown"}>
              {instance.work_dir || "Workspace unknown"}
            </span>
          </p>
        )}
        <p className="mt-0.5 text-xs text-gray-400" title={instance?.task_id || ""}>
          {instance ? (instance.task_id || "Idle — waiting to claim work") : "This pool has no running worker."}
        </p>
        {instance && <p className="text-xs">Session: {instance.id}</p>}
        <div role="tablist" aria-label={title + " view"} className="flex gap-2">
          {tabs.map(({ id: key, label, Icon }) => (
            <button key={key} type="button" role="tab" id={id + "-" + key} data-primary-control
              aria-controls={id + "-panel"} aria-selected={tab === key} onClick={() => setTab(key)}
              className={"flex items-center gap-1.5 rounded border px-2 py-1 text-xs "
                + (tab === key ? "border-indigo-400/60 bg-indigo-500/10 text-indigo-200" : "border-transparent text-gray-400 hover:text-gray-200")}>
              <Icon aria-hidden="true" className="h-3.5 w-3.5" />{label}
            </button>
          ))}
        </div>
      </>}>
      <div role="tabpanel" id={id + "-panel"} aria-labelledby={id + "-title"} className="min-h-0 flex-1 overflow-hidden">
        {tab === "terminal" ? <PoolInstanceTerminal instance={instance} focusRequest={focusRequest} /> : (
          <div aria-label={title + " settings"} className="h-full space-y-4 overflow-auto p-4">
            <p className="text-xs leading-relaxed text-gray-400">
              Lifecycle: <span className="text-gray-200">pool</span>. The daemon sizes this pool
              between the bounds below; individual instances are started and drained
              automatically and cannot be added or deleted by hand. Bounds are fleet-wide: the
              placer decides which project each authorised start lands in.
            </p>
            <PoolScaleFields pool={pool} />
            <PoolProjects projects={projects} />
          </div>
        )}
      </div>
      </TerminalPane>
    </section>
  );
}
