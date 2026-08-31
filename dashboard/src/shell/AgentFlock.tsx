import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDownIcon, ChevronRightIcon, UsersIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useAgentFlock } from "../api/agents";
import { useAgentSelection } from "../pages/agents/useAgentSelection";
import { AgentState, AgentEligibility, AgentWaitingQuestion } from "../pages/agents/AgentMetadata";

const COLLAPSED_KEY = "aq:flock:collapsed";

export default function AgentFlock() {
  const { data: agents = [], isLoading, error, refetch } = useAgentFlock();
  const selection = useAgentSelection();
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
          <span className="ml-auto font-mono text-[10px] text-gray-500">{agents.length}</span>
        </button>
        <Link to="/agents" data-listnav="1" aria-label="Manage agent flock" title="Manage agents"
          className="rounded p-2 text-gray-500 hover:bg-gray-800 hover:text-gray-200">
          <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
        </Link>
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
          {!isLoading && !error && agents.length === 0 && (
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
