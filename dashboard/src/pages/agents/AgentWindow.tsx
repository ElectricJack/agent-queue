import { useEffect, useId, useState } from "react";
import { XMarkIcon, CommandLineIcon, Cog6ToothIcon, ChatBubbleLeftRightIcon } from "@heroicons/react/24/outline";
import { useSearchParams } from "react-router-dom";
import type { FlockAgent } from "../../api/agents";
import { AgentSubagents, AgentState, AgentEligibility } from "./AgentMetadata";
import AgentSettings from "./AgentSettings";
import AgentTerminal from "./AgentTerminal";
import GlobalChat from "../GlobalChat";

export default function AgentWindow({ agent, onClose, resetToken, focusRequest }: {
  agent: FlockAgent;
  onClose: () => void;
  resetToken: string | null;
  focusRequest: string | null;
}) {
  const [localTab, setLocalTab] = useState<"terminal" | "settings" | "conversations">("terminal");
  const [searchParams, setSearchParams] = useSearchParams();
  const globalSupervisor = agent.role === "supervisor" && agent.project_id == null;
  const requestedTab = searchParams.get("supervisorView");
  const tab = globalSupervisor
    ? requestedTab === "conversations" || (requestedTab === null && searchParams.has("conversation"))
      ? "conversations" : requestedTab === "settings" ? "settings" : "terminal"
    : localTab;
  const setTab = (next: typeof localTab) => {
    if (!globalSupervisor) {
      setLocalTab(next);
      return;
    }
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      params.set("supervisorView", next);
      return params;
    });
  };
  const id = useId();
  useEffect(() => {
    if (resetToken || focusRequest) setLocalTab("terminal");
  }, [resetToken, focusRequest]);

  const tabs = [
    { id: "terminal" as const, label: "Terminal", Icon: CommandLineIcon },
    ...(globalSupervisor ? [{ id: "conversations" as const, label: "Conversations", Icon: ChatBubbleLeftRightIcon }] : []),
    { id: "settings" as const, label: "Settings", Icon: Cog6ToothIcon },
  ];

  return (
    <section aria-label={agent.name + " agent window"}
      className="flex min-h-80 min-w-0 flex-col overflow-hidden rounded-xl border border-gray-800 bg-gray-900/40 lg:min-h-0">
      <header className="shrink-0 border-b border-gray-800 bg-gray-900 px-3 py-2">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-sm font-semibold text-gray-100">{agent.name}</h2>
              <span className="text-[10px] capitalize text-gray-500"><AgentState agent={agent} /></span>
              {agent.role === "supervisor" && <span className="rounded bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-300">Supervisor</span>}
            </div>
            <p className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-gray-500">
              <span className="min-w-0 truncate text-xs text-gray-400" title={(agent.provider || "Provider unknown") + " · " + (agent.model || "Model unknown")}>
                {agent.provider || "Provider unknown"} · {agent.model || "Model unknown"}
              </span>
              <span>Intelligence: {agent.intelligence_class || "Unknown"}</span>
              <AgentSubagents agent={agent} />
              <AgentEligibility agent={agent} />
            </p>
            <p className="mt-0.5 truncate text-xs text-gray-400" title={agent.current_task_title || agent.current_task_id || ""}>
              {agent.current_task_title || agent.current_task_id || "Idle — no assigned task"}
            </p>
            {agent.session_id && !agent.current_task_id && agent.project_id && (
              <p className="mt-0.5 truncate text-xs text-indigo-300">Attached project: {agent.project_id}</p>
            )}
          </div>
          <div className="flex max-w-full shrink-0 items-center gap-3">
            <div role="tablist" aria-label={agent.name + " view"} className="flex min-w-0 flex-wrap gap-2">
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
            <button type="button" aria-label={"Close " + agent.name + " view"} title="Close view (agent keeps running)" onClick={onClose}
              className="shrink-0 rounded p-1 text-gray-500 hover:bg-gray-800 hover:text-gray-100">
              <XMarkIcon className="h-4 w-4" />
            </button>
          </div>
        </div>
      </header>
      <div role="tabpanel" id={id + "-panel"} aria-labelledby={id + "-" + tab} className="min-h-0 flex-1 overflow-hidden">
        {tab === "terminal" ? <AgentTerminal agent={agent} focusRequest={focusRequest} />
          : tab === "conversations" ? <div className="h-full p-3"><GlobalChat /></div>
            : <AgentSettings agent={agent} onDeleted={onClose} />}
      </div>
    </section>
  );
}
