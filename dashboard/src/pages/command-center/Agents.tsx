import { useState } from "react";
import { useAgentFlock } from "../../api/agents";
import { useListNav } from "../../shell/hotkeys/useListNav";
import { MAX_AGENT_VIEWS, useAgentSelection } from "../agents/useAgentSelection";
import { AgentState, AgentEligibility } from "../agents/AgentMetadata";
import AgentConsoleTile from "./AgentConsoleTile";

export default function CommandCenterAgents() {
  const { data: agents = [], isLoading, error } = useAgentFlock();
  const { select } = useAgentSelection();
  const bodyRef = useListNav<HTMLTableSectionElement>({ axis: "vertical" });
  const [view, setView] = useState<"table" | "grid">("table");
  const running = agents.filter((agent) => agent.session_id && (agent.session_state === "running" || agent.session_state === "draining") && agent.session_provider === "tmux");
  const tiles = running.slice(0, MAX_AGENT_VIEWS);
  const hidden = running.length - tiles.length;

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300">Agents</h2>
        <div className="flex overflow-hidden rounded border border-gray-800 text-xs">
          {(["table", "grid"] as const).map((value) => (
            <button key={value} type="button" onClick={() => setView(value)} aria-pressed={view === value}
              className={"px-2 py-1 capitalize " + (view === value ? "bg-gray-700 text-white" : "text-gray-400")}>
              {value}
            </button>
          ))}
        </div>
      </div>
      {error && <p role="alert" className="text-sm text-red-300">{error.message}</p>}
      {view === "grid" && (
        <div className="space-y-2">
          {tiles.length === 0 && <p className="text-sm text-gray-500">{isLoading ? "Loading…" : "No running tmux agents."}</p>}
          <div className="grid gap-3 sm:grid-cols-2">
            {tiles.map((agent) => (
              <AgentConsoleTile key={agent.id} sessionId={agent.session_id!} title={agent.name}
                onOpen={() => select(agent.id)} />
            ))}
          </div>
          {hidden > 0 && (
            <p className="text-xs text-amber-400">
              +{hidden} more running {hidden === 1 ? "agent" : "agents"} not shown (live view is capped at {MAX_AGENT_VIEWS}).
              {" "}Select agents from the flock to choose your views.
            </p>
          )}
        </div>
      )}
      {view === "table" && (
        <div className="overflow-x-auto rounded border border-gray-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-900 text-xs uppercase text-gray-500">
              <tr>
                <th className="px-3 py-2">Name</th>
                <th className="px-3 py-2">State</th>
                <th className="px-3 py-2">Assigned project</th>
                <th className="px-3 py-2">Task</th>
                <th className="px-3 py-2">Session</th>
              </tr>
            </thead>
            <tbody ref={bodyRef} className="divide-y divide-gray-800">
              {isLoading && <tr><td colSpan={5} className="px-3 py-4 text-gray-500">Loading…</td></tr>}
              {!isLoading && agents.length === 0 && <tr><td colSpan={5} className="px-3 py-4 text-center text-gray-500">No agents.</td></tr>}
              {agents.map((agent) => (
                <tr key={agent.id} tabIndex={0} data-listnav="1"
                  onClick={(event) => select(agent.id, event.shiftKey)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      select(agent.id, event.shiftKey);
                    }
                  }}
                  className="cursor-pointer hover:bg-gray-900/50 focus:bg-gray-900/50 focus:outline-none">
                  <td className="px-3 py-2 text-gray-200">{agent.name}</td>
                  <td className="px-3 py-2 text-gray-300">
                    <span><AgentState agent={agent} /></span>
                    <AgentEligibility agent={agent} />
                  </td>
                  <td className="px-3 py-2 text-gray-400">{agent.current_project_id || "—"}</td>
                  <td className="px-3 py-2 text-gray-400">{agent.current_task_title || agent.current_task_id || "—"}</td>
                  <td className="px-3 py-2 text-gray-400">
                    {agent.session_id
                      ? <span className="font-mono text-xs">{agent.session_id.slice(0, 8)} · {agent.session_state}</span>
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
