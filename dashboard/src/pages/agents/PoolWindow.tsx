import { useId } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { CommandLineIcon, Cog6ToothIcon } from "@heroicons/react/24/outline";
import { PoolInstanceTerminal } from "./AgentTerminal";
import { PoolBadge, PoolOutsidePools, PoolPlacementRow, PoolQuarantine, PoolSupplyRow } from "./PoolMetadata";
import PoolProjects from "./PoolProjects";
import PoolScaleFields from "./PoolScaleFields";
import PoolNameFields from "./PoolNameFields";
import { formatIdle, poolDisplayName, type PoolEntry } from "./pools";
import TerminalPane, { TerminalTabs } from "../../components/TerminalPane";

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
      <span className={compact ? "sr-only" : undefined}>{compact ? "Terminal for " + poolDisplayName(entry.pool) + " pool" : "Instance"}</span>
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
export default function PoolWindow({ entry, instanceId, onInstanceChange, onClose, focusRequest }: {
  entry: PoolEntry;
  instanceId: string | null;
  onInstanceChange: (instanceId: string | null) => void;
  onClose: () => void;
  focusRequest: string | null;
}) {
  const [params, setParams] = useSearchParams();
  const tab = params.get("pool-view") === "settings" ? "settings" : "terminal";
  const setTab = (next: "terminal" | "settings") => {
    const search = new URLSearchParams(params);
    if (next === "settings") search.set("pool-view", next);
    else search.delete("pool-view");
    setParams(search);
  };
  const id = useId();

  const { pool, projects, instances } = entry;
  // A pinned instance can drain away between polls; fall back to the pool's
  // oldest live session rather than blanking the view.
  const instance = instances.find((row) => row.id === instanceId) ?? instances[0] ?? null;
  const title = poolDisplayName(pool) + " pool";

  const tabs = [
    { id: "terminal" as const, label: "Terminal", Icon: CommandLineIcon },
    { id: "settings" as const, label: "Settings", Icon: Cog6ToothIcon },
  ];

  return (
    <section aria-label={title + " agent window"}
      className="flex min-h-96 min-w-0 flex-col overflow-hidden rounded-xl border border-gray-800 bg-gray-900/40 lg:min-h-0">
      <TerminalPane title={title} status={instance?.stalled ? "Stalled" : instance?.state || "Idle"} onClose={onClose} titleId={id + "-title"}
        primary={instances.length > 1 ? <InstancePicker entry={entry} instance={instance} onChange={onInstanceChange} compact /> : undefined}
        tabs={<TerminalTabs label={title + " view"} idPrefix={id} tabs={tabs} value={tab} onChange={setTab} />}
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
      </>}>
      <div role="tabpanel" id={id + "-panel"} aria-labelledby={id + "-title"} className="min-h-0 flex-1 overflow-hidden">
        {tab === "terminal" ? <PoolInstanceTerminal instance={instance} focusRequest={focusRequest} /> : (
          <div aria-label={title + " settings"} className="h-full space-y-4 overflow-auto p-4">
            <Link to="/agents" className="inline-block text-xs text-indigo-300 hover:underline">Back to pools</Link>
            <PoolNameFields pool={pool} />
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
