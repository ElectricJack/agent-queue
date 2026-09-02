import { useId, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, UsersIcon, PlusIcon } from "@heroicons/react/24/outline";
import { useAgentFlock, useFlockSubagents } from "../api/agents";
import { useAgentSelection } from "../pages/agents/useAgentSelection";
<<<<<<< HEAD
import { AgentState, AgentEligibility, AgentWaitingQuestion, FlockSubagents } from "../pages/agents/AgentMetadata";
import { PoolBadge, PoolQuarantine, PoolSupplyRow } from "../pages/agents/PoolMetadata";
import { isPoolAgent, usePoolFlock } from "../pages/agents/pools";

const COLLAPSED_KEY = "aq:flock:collapsed";

export default function AgentFlock() {
  const { data: roster = [], isLoading, error, refetch } = useAgentFlock();
  const { data: subagents } = useFlockSubagents();
  const selection = useAgentSelection();
  const { entries: pools, poolIds } = usePoolFlock();
  // Pool members are reachable through their pool entry; listing each
  // ephemeral instance row here as well would double-count the flock.
  const agents = roster.filter((agent) => !isPoolAgent(agent, poolIds));
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(COLLAPSED_KEY) === "true"; }
    catch { return false; }
  });
  const [limitAt, setLimitAt] = useState<string | null>(null);
  const listId = useId();
  const Chevron = collapsed ? ChevronRightIcon : ChevronDownIcon;

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    try { localStorage.setItem(COLLAPSED_KEY, String(next)); }
    catch { /* The current view still works when storage is unavailable. */ }
  };

  return (
    <section aria-label="Agent flock">
      <div className="mb-1 flex items-center gap-1">
        <button
          type="button"
          data-listnav="1"
          onClick={toggle}
          aria-expanded={!collapsed}
          aria-controls={listId}
          className="flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-2 text-left text-xs font-semibold uppercase tracking-wide text-gray-400 hover:bg-gray-800 hover:text-gray-100"
        >
          <Chevron className="h-3 w-3" />
          <UsersIcon className="h-4 w-4" />
          <span>Agent flock</span>
          <span className="ml-auto flex items-center gap-1.5">
            <FlockSubagents rollup={subagents} />
            <span className="font-mono text-[10px] text-gray-500">{agents.length + pools.length}</span>
          </span>
        </button>
        <button type="button" data-listnav="1" aria-label="Add agent" title="Add agent"
          onClick={() => selection.setAdding(true)}
          className="rounded p-2 text-gray-500 hover:bg-gray-800 hover:text-gray-200">
          <PlusIcon className="h-3.5 w-3.5" />
        </button>
      </div>
      {!collapsed && (
        <div id={listId} className="space-y-1">
          <p className="px-3 pb-1 text-[10px] text-gray-500">Shift-click to tile up to four agents</p>
          {isLoading && <p className="px-3 py-2 text-xs text-gray-500">Loading agents…</p>}
          {error && (
            <div className="px-3 py-2 text-xs text-red-300" role="alert">
              Could not load agents. <button type="button" className="underline" onClick={() => void refetch()}>Retry</button>
            </div>
          )}
          {!isLoading && !error && agents.length === 0 && pools.length === 0 && (
            <p className="px-3 py-2 text-xs text-gray-500">No agents defined.</p>
          )}
          {agents.map((agent) => {
            const selected = selection.selectedIds.includes(agent.id);
            const descriptionId = listId + "-" + agent.id;
            return (
              <button
                key={agent.id}
                type="button"
                data-listnav="1"
                aria-label={"Open " + agent.name}
                aria-describedby={descriptionId}
                aria-pressed={selected}
                onClick={(event) => setLimitAt(selection.select(agent.id, event.shiftKey) ? null : selection.locationKey)}
                className={"block w-full rounded-lg border px-3 py-1.5 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400 "
                  + (selected ? "border-indigo-500/40 bg-indigo-500/10" : "border-transparent hover:border-gray-700 hover:bg-gray-800/70")}
              >
                <span className="mb-0.5 flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-gray-200">{agent.name}</span>
                  <span className={"shrink-0 text-[10px] capitalize " + (agent.waiting_question ? "text-amber-300" : agent.state === "busy" ? "text-emerald-400" : "text-gray-500")}>
                    <AgentState agent={agent} />
                  </span>
                </span>
                <span id={descriptionId} className="block space-y-0.5 text-[10px] leading-tight text-gray-500">
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="min-w-0 truncate text-gray-400" title={(agent.provider || "Provider unknown") + " · " + (agent.model || "Model unknown")}>
                      {agent.provider || "Provider unknown"} · {agent.model || "Model unknown"}
                    </span>
                    <span className="shrink-0" title={agent.intelligence_class || "Intelligence level unknown"}>
                      int: {agent.intelligence_class || "Unknown"}
                    </span>
                  </span>
                  <AgentEligibility agent={agent} />
                  <AgentWaitingQuestion agent={agent} />
                  <span className="block truncate text-gray-400" title={agent.current_task_title || agent.current_task_id || "Idle — no assigned task"}>
                    {agent.current_task_title || agent.current_task_id || "Idle — no assigned task"}
                  </span>
                </span>
              </button>
            );
          })}
          {pools.map((entry) => {
            const selected = selection.selectedIds.some((id) => id.split("@")[0] === entry.key);
            const descriptionId = listId + "-" + entry.key;
            return (
              <button
                key={entry.key}
                type="button"
                data-listnav="1"
                aria-label={"Open " + entry.profileId + " pool"}
                aria-describedby={descriptionId}
                aria-pressed={selected}
                onClick={(event) => setLimitAt(selection.select(entry.key, event.shiftKey) ? null : selection.locationKey)}
                className={"block w-full rounded-lg border px-3 py-1.5 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400 "
                  + (selected ? "border-indigo-500/40 bg-indigo-500/10" : "border-transparent hover:border-gray-700 hover:bg-gray-800/70")}
              >
                <span className="mb-0.5 flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-gray-200">{entry.profileId}</span>
                  <PoolBadge />
                </span>
                <span id={descriptionId} className="block space-y-0.5 text-[10px] leading-tight text-gray-500">
                  <span className="block truncate" title={entry.projectId}>{entry.projectId}</span>
                  <PoolSupplyRow pool={entry.pool} />
                  <PoolQuarantine pool={entry.pool} />
                  <span className="block truncate text-gray-400">
                    {entry.instances.length === 1 ? "1 live instance" : entry.instances.length + " live instances"}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
      {limitAt === selection.locationKey && (
        <p role="status" aria-label="Agent view limit" className="mt-2 px-3 text-xs text-amber-300">
          Four agent views are already open. Close a view to add another.
        </p>
      )}
    </section>
  );
}
